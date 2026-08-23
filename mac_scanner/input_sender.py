"""Synthetic key sender — anti-idle support for PS Remote Play.

Posts macOS key-down/key-up events through CoreGraphics so the in-game
character moves (default: spacebar = jump) often enough that the UEFN
island doesn't drop the player for being idle.

Why CGEventPost and not pynput.keyboard.Controller?
  pynput uses CGEventPost under the hood on macOS, but going direct
  lets us:
    * use kCGHIDEventTap (the lowest tap level) so the event reaches
      PS Remote Play even though our own Tk window is also a foreground
      app at times,
    * control the down→up hold time precisely (Remote Play occasionally
      drops 0-ms taps),
    * keep this module self-contained for unit testing without pulling
      in pynput's listener machinery.

Permissions:
  * macOS Accessibility for the parent terminal/Python — without it
    `CGEventPost` is silently no-op'd.
  * If you launch from a wrapper (e.g. tmux, an IDE), make sure THAT
    process is the one granted Accessibility.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import config


_LOGGER = logging.getLogger("mac_scanner.input_sender")


# macOS virtual key codes — HIToolbox/Events.h. Stable across keyboard
# layouts (kVK_Space is always 49, etc.). Add to this map to support
# new key names; values are the kVK_* constants.
KEY_CODES: dict[str, int] = {
    "space": 49,
    "return": 36,
    "enter": 36,
    "escape": 53,
    "tab": 48,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5,
    "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
}


def _import_quartz():
    import Quartz  # type: ignore[import]
    return Quartz


def is_accessibility_trusted() -> bool:
    """Return True if our process can post synthetic input events.

    Maps to macOS's `AXIsProcessTrusted()`. When this returns False,
    `CGEventPost` is silently no-op'd — the call returns normally and
    we have no other indication the keystroke never landed, which is
    why this diagnostic is the FIRST thing to check whenever
    `--send-space` "succeeds" but the game doesn't move.

    Remediation: System Settings → Privacy & Security → Accessibility,
    add (or *remove and re-add*, after a venv recreation) the binary
    listed in `sys.executable`. Quit the terminal fully and reopen.
    """
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore[import]
    except ImportError:  # pragma: no cover — only on non-macOS
        return False
    try:
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def frontmost_application() -> dict | None:
    """Return {'name': str, 'bundle_id': str|None, 'pid': int} for the
    frontmost app, or None if it can't be determined.

    Used right after activation to verify Remote Play actually came to
    the front before we post the key — AppleScript's `activate` can
    silently no-op when the target app is hidden, sandboxed, or
    blocked by Stage Manager / Mission Control.
    """
    try:
        from AppKit import NSWorkspace  # type: ignore[import]
    except ImportError:
        return None
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return {
            "name": str(app.localizedName() or ""),
            "bundle_id": str(app.bundleIdentifier() or "") or None,
            "pid": int(app.processIdentifier()),
        }
    except Exception:
        return None


# NSApplicationActivationOptions: bring app + all its windows forward,
# ignoring whatever app is currently frontmost. Equivalent to clicking
# the app's Dock icon.
_NS_ACTIVATE_IGNORING_OTHER_APPS: int = 1 << 1


def _find_remote_play_running_app(owner_names: tuple[str, ...]):
    """Return the NSRunningApplication for PS Remote Play, or None.

    Iterates `runningApplications()` and matches `localizedName()`
    against the configured candidate names. Cheap — no AppleScript and
    no permissions beyond what `runningApplications()` already grants
    (which is "nothing extra" on macOS).
    """
    try:
        from AppKit import NSWorkspace  # type: ignore[import]
    except ImportError:  # pragma: no cover
        return None
    wanted_lower = {n.lower() for n in owner_names}
    try:
        apps = NSWorkspace.sharedWorkspace().runningApplications() or []
    except Exception:
        return None
    for app in apps:
        try:
            name = str(app.localizedName() or "").lower()
        except Exception:
            continue
        if name in wanted_lower:
            return app
    return None


def activate_remote_play(
    owner_names: tuple[str, ...] = config.REMOTE_PLAY_OWNER_NAMES,
    *,
    verify: bool = True,
    verify_settle_seconds: float = 0.4,
) -> bool:
    """Bring PS Remote Play to the foreground.

    Strategy:
      1. Look up the running NSRunningApplication for one of `owner_names`
         and call `.activateWithOptions_(IgnoringOtherApps)` on it. This
         needs Accessibility (which we already require to post keys) but
         does NOT need the separate Automation / Apple Events grant that
         `osascript` requires.
      2. If PyObjC isn't usable, fall back to `osascript`.

    With `verify=True` (default), check `NSWorkspace.frontmostApplication`
    after activation and return True only if it matches `owner_names`.
    This catches the silent-failure case where macOS allows the API
    call but Stage Manager / Mission Control prevents the focus
    transition.
    """
    wanted_lower = {n.lower() for n in owner_names}

    # --- Primary path: NSRunningApplication.activate -------------------
    app = _find_remote_play_running_app(owner_names)
    if app is not None:
        try:
            ok = bool(app.activateWithOptions_(_NS_ACTIVATE_IGNORING_OTHER_APPS))
            _LOGGER.debug(
                "NSRunningApplication.activate(%r) returned %s",
                str(app.localizedName()), ok,
            )
        except Exception as e:
            _LOGGER.warning("NSRunningApplication.activate failed: %s", e)
            ok = False
        if ok:
            if not verify:
                return True
            time.sleep(verify_settle_seconds)
            front = frontmost_application()
            if front is not None and front["name"].lower() in wanted_lower:
                return True
            _LOGGER.warning(
                "activateWithOptions_ returned True but frontmost is %r; "
                "trying AppleScript fallback",
                front and front.get("name"),
            )
    else:
        _LOGGER.debug(
            "no NSRunningApplication matched %s — Remote Play not running?",
            list(owner_names),
        )

    # --- Fallback: osascript (needs Automation permission) -------------
    for name in owner_names:
        try:
            r = subprocess.run(
                ["osascript", "-e", f'tell application "{name}" to activate'],
                check=False, capture_output=True, text=True, timeout=2.0,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            _LOGGER.warning("osascript activate %r failed: %s", name, e)
            continue
        if r.returncode != 0:
            continue
        if not verify:
            return True
        time.sleep(verify_settle_seconds)
        front = frontmost_application()
        if front is not None and front["name"].lower() in wanted_lower:
            return True
        _LOGGER.warning(
            "AppleScript activate %r returned 0 but frontmost is %r — "
            "Automation permission missing? (System Settings → Privacy & "
            "Security → Automation → enable PS Remote Play under your "
            "terminal/IDE)",
            name, front and front.get("name"),
        )

    _LOGGER.warning(
        "could not bring any of %s to the front", list(owner_names),
    )
    return False


def send_keypress(
    key: str = "space",
    *,
    focus_remote_play: bool = True,
    hold_seconds: float = 0.05,
    activation_delay: float = 0.35,
) -> bool:
    """Post a single key down/up to whichever app is frontmost.

    With `focus_remote_play=True`, activate Remote Play first and wait
    `activation_delay` seconds for the OS focus transition to settle.
    Returns True if CGEventPost did not raise and Accessibility is
    granted; CGEventPost will silently no-op (and STILL return success
    at the C level) without Accessibility, so we explicitly check
    `AXIsProcessTrusted()` here and warn loudly when it's False.

    Raises ValueError on unknown `key`. Raises RuntimeError if Quartz
    cannot be imported (e.g. running this off macOS).
    """
    key = key.lower()
    if key not in KEY_CODES:
        raise ValueError(
            f"unknown key {key!r}; supported: {sorted(KEY_CODES)}"
        )

    try:
        Quartz = _import_quartz()
    except ImportError as e:  # pragma: no cover — macOS only
        raise RuntimeError(f"Quartz unavailable: {e}") from e

    trusted = is_accessibility_trusted()
    if not trusted:
        _LOGGER.warning(
            "AXIsProcessTrusted() is False — CGEventPost will be silently "
            "dropped. Grant Accessibility to %s in System Settings → "
            "Privacy & Security → Accessibility, quit the terminal "
            "fully, reopen, then retry. Continuing anyway in case the "
            "OS lifted the restriction since process start.",
            sys.executable if "sys" in globals() else "this Python binary",
        )

    activated: bool | None = None
    if focus_remote_play:
        activated = activate_remote_play()
        if not activated:
            _LOGGER.warning(
                "could not activate Remote Play; sending %r to current "
                "frontmost app instead", key,
            )
        time.sleep(activation_delay)

    keycode = KEY_CODES[key]
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    if hold_seconds > 0:
        time.sleep(hold_seconds)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    front = frontmost_application() if focus_remote_play else None
    _LOGGER.info(
        "sent keypress %r (code=%d, hold=%.2fs, focus_first=%s, "
        "activated=%s, frontmost=%s, trusted=%s)",
        key, keycode, hold_seconds, focus_remote_play, activated,
        front and front.get("name"), trusted,
    )
    return trusted


def auto_grab(
    key: str = "e",
    *,
    duration_seconds: float = 3.0,
    delay_seconds: float = 0.1,
    hold_seconds: float = 0.03,
    focus_remote_play: bool = True,
    activation_delay: float = 0.35,
    human_monitor=None,
) -> int:
    """Spam `key` for `duration_seconds` with `delay_seconds` between presses.

    Each iteration is `key down → sleep(hold_seconds) → key up →
    sleep(delay_seconds)`. The loop exits when the wall-clock deadline
    is reached, returning the number of full presses fired.

    The first iteration is preceded by an `activate_remote_play()` +
    settle when `focus_remote_play=True`. We DO NOT re-activate
    between presses — that would add ~400ms per iteration (settle
    delay) and destroy the spam cadence. Once Remote Play is
    frontmost, every subsequent CGEventPost on the HID tap goes to it.

    If `human_monitor` is supplied, we extend its synth-suppression
    window over the entire burst (plus 1s slack), so the OCR-side
    pause-on-input toggle doesn't fire on our own keystrokes.
    """
    key = key.lower()
    if key not in KEY_CODES:
        raise ValueError(
            f"unknown key {key!r}; supported: {sorted(KEY_CODES)}"
        )
    if duration_seconds <= 0 or delay_seconds < 0:
        raise ValueError(
            f"invalid timing — duration={duration_seconds}, delay={delay_seconds}"
        )

    try:
        Quartz = _import_quartz()
    except ImportError as e:  # pragma: no cover — macOS only
        raise RuntimeError(f"Quartz unavailable: {e}") from e

    trusted = is_accessibility_trusted()
    if not trusted:
        _LOGGER.warning(
            "AXIsProcessTrusted() is False — auto_grab presses will be "
            "silently dropped. Grant Accessibility to %s.",
            sys.executable,
        )

    activated: bool | None = None
    if focus_remote_play:
        activated = activate_remote_play()
        if not activated:
            _LOGGER.warning(
                "auto_grab: could not bring Remote Play to the front; "
                "spam will land in whatever app is currently frontmost",
            )
        time.sleep(activation_delay)

    if human_monitor is not None:
        try:
            human_monitor.mark_synth_window(duration_seconds + 1.0)
        except Exception:
            pass

    keycode = KEY_CODES[key]
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    deadline = time.perf_counter() + duration_seconds
    count = 0
    while True:
        now = time.perf_counter()
        if now >= deadline:
            break
        down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
        up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        if hold_seconds > 0:
            time.sleep(hold_seconds)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        count += 1
        # Sleep the gap, but stop at the deadline rather than overshooting.
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        time.sleep(min(delay_seconds, remaining))

    front = frontmost_application() if focus_remote_play else None
    _LOGGER.info(
        "auto_grab finished: key=%r presses=%d duration=%.2fs delay=%.2fs "
        "(activated=%s, frontmost=%s, trusted=%s)",
        key, count, duration_seconds, delay_seconds, activated,
        front and front.get("name"), trusted,
    )
    return count


# After the interact burst, wait then post a single "f" (game-specific action).
POST_AUTO_GRAB_FOLLOWUP_DELAY_SECONDS: float = 0.5
# Extra synth-suppression tail so pause-on-human-input ignores the follow-up key.
_POST_AUTO_GRAB_SYNTH_TAIL_SECONDS: float = 2.0


@dataclass(frozen=True)
class AutoGrabConfig:
    key: str = "e"
    duration_seconds: float = 3.0
    delay_seconds: float = 0.1
    hold_seconds: float = 0.03
    focus_remote_play: bool = True

    @classmethod
    def from_config_module(cls) -> "AutoGrabConfig":
        return cls(
            key=config.AUTO_GRAB_KEY,
            duration_seconds=config.AUTO_GRAB_DURATION_SECONDS,
            delay_seconds=config.AUTO_GRAB_DELAY_SECONDS,
            hold_seconds=config.AUTO_GRAB_HOLD_SECONDS,
            focus_remote_play=config.AUTO_GRAB_FOCUS_REMOTE_PLAY,
        )


class AutoGrabber:
    """Thread-safe wrapper around `auto_grab()` for use inside a running scanner.

    Usage from the capture loop:

        grabber = AutoGrabber(cfg, human_monitor=mon, on_log=log)
        ...
        # On a card hit:
        if not grabber.is_active:
            grabber.grab_async()

    `grab_async()` returns immediately and runs the burst in a daemon
    thread. Re-entry is suppressed: a second `grab_async()` call while
    a burst is still firing returns False instead of starting a parallel
    burst that would corrupt the per-press cadence.
    """

    def __init__(
        self,
        cfg: AutoGrabConfig | None = None,
        *,
        human_monitor=None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._cfg = cfg or AutoGrabConfig.from_config_module()
        self._human = human_monitor
        self._on_log = on_log
        self._lock = threading.Lock()
        self._active_thread: threading.Thread | None = None
        self._grabs: int = 0
        self._last_grab_at: float = 0.0

    @property
    def config(self) -> AutoGrabConfig:
        return self._cfg

    @property
    def grabs(self) -> int:
        return self._grabs

    @property
    def is_active(self) -> bool:
        with self._lock:
            t = self._active_thread
            return t is not None and t.is_alive()

    def _log(self, msg: str) -> None:
        _LOGGER.info(msg)
        if self._on_log is not None:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def grab_now(self) -> int:
        """Synchronous fire. Blocks until the burst completes. Returns press count."""
        count = auto_grab(
            self._cfg.key,
            duration_seconds=self._cfg.duration_seconds,
            delay_seconds=self._cfg.delay_seconds,
            hold_seconds=self._cfg.hold_seconds,
            focus_remote_play=self._cfg.focus_remote_play,
            human_monitor=self._human,
        )
        if self._human is not None:
            try:
                self._human.mark_synth_window(_POST_AUTO_GRAB_SYNTH_TAIL_SECONDS)
            except Exception:
                pass
        time.sleep(POST_AUTO_GRAB_FOLLOWUP_DELAY_SECONDS)
        send_keypress(
            "f",
            focus_remote_play=False,
            hold_seconds=self._cfg.hold_seconds,
        )
        _LOGGER.info(
            "auto-grab follow-up: key=%r delay=%.2fs",
            "f",
            POST_AUTO_GRAB_FOLLOWUP_DELAY_SECONDS,
        )
        self._grabs += 1
        self._last_grab_at = time.perf_counter()
        return count

    def grab_async(self, *, label: str = "") -> bool:
        """Start a grab burst in a daemon thread. Returns False if a
        burst is already in flight (call is a no-op)."""
        with self._lock:
            if self._active_thread is not None and self._active_thread.is_alive():
                return False
            t = threading.Thread(
                target=self._run, args=(label,),
                daemon=True, name="mac_scanner.auto_grab",
            )
            self._active_thread = t
        t.start()
        return True

    def _run(self, label: str) -> None:
        prefix = f"[auto-grab{(' ' + label) if label else ''}]"
        self._log(
            f"{prefix} starting — key={self._cfg.key!r} "
            f"duration={self._cfg.duration_seconds}s "
            f"delay={self._cfg.delay_seconds}s"
        )
        try:
            count = self.grab_now()
            self._log(f"{prefix} done — {count} presses fired (#bursts={self._grabs})")
        except Exception as e:
            self._log(f"{prefix} failed: {e}")


@dataclass(frozen=True)
class AntiIdleConfig:
    key: str = "space"
    interval_seconds: float = 120.0
    focus_remote_play: bool = True
    hold_seconds: float = 0.05
    # Skip a fire if the user touched a real key/mouse within this window.
    human_grace_seconds: float = 10.0

    @classmethod
    def from_config_module(cls) -> "AntiIdleConfig":
        return cls(
            key=config.ANTI_IDLE_KEY,
            interval_seconds=config.ANTI_IDLE_INTERVAL_SECONDS,
            focus_remote_play=config.ANTI_IDLE_FOCUS_REMOTE_PLAY,
            hold_seconds=config.ANTI_IDLE_KEY_HOLD_SECONDS,
            human_grace_seconds=config.ANTI_IDLE_HUMAN_GRACE_SECONDS,
        )


class AntiIdleSender:
    """Background thread that fires a key every `interval_seconds`.

    Lifecycle:
      sender = AntiIdleSender(cfg, human_monitor=...)
      sender.start()           # starts thread, but does nothing yet
      sender.set_enabled(True) # actually begin firing
      ...
      sender.stop()            # joinable from outside, idempotent

    Why the start/enabled split? The capture loop has the same shape:
    the worker thread always exists, and a flag controls whether it
    does real work. Lets a UI checkbox toggle anti-idle on/off without
    juggling threads.

    The sender calls `human_monitor.mark_synth_window(...)` before each
    post so pynput's event tap on the OCR side doesn't see our synthetic
    space as "real user input" and trigger pause-on-input.
    """

    def __init__(
        self,
        cfg: AntiIdleConfig | None = None,
        *,
        human_monitor=None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._cfg = cfg or AntiIdleConfig.from_config_module()
        self._human = human_monitor
        self._on_log = on_log
        self._stop = threading.Event()
        self._enabled = False
        self._thread: threading.Thread | None = None
        self._last_send_at: float = 0.0
        self._sends: int = 0

    @property
    def sends(self) -> int:
        return self._sends

    @property
    def config(self) -> AntiIdleConfig:
        return self._cfg

    def _log(self, msg: str) -> None:
        _LOGGER.info(msg)
        if self._on_log is not None:
            try:
                self._on_log(msg)
            except Exception:
                pass

    def set_enabled(self, on: bool) -> None:
        already = self._enabled
        self._enabled = on
        if on and not already:
            self._log(
                f"[anti-idle] enabled — key={self._cfg.key!r} "
                f"every {self._cfg.interval_seconds:.0f}s "
                f"(focus_remote_play={self._cfg.focus_remote_play}, "
                f"grace={self._cfg.human_grace_seconds:.0f}s)"
            )
        elif already and not on:
            self._log("[anti-idle] disabled")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="mac_scanner.anti_idle",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def fire_once(self) -> bool:
        """Send one keypress immediately. Used by `--send-space`."""
        if self._human is not None:
            try:
                # The synth event reaches our own pynput listener too;
                # suppress it for slightly longer than the hold time.
                self._human.mark_synth_window(self._cfg.hold_seconds + 1.0)
            except Exception:
                pass
        ok = send_keypress(
            self._cfg.key,
            focus_remote_play=self._cfg.focus_remote_play,
            hold_seconds=self._cfg.hold_seconds,
        )
        self._last_send_at = time.perf_counter()
        self._sends += 1
        return ok

    def _human_input_recent(self) -> bool:
        if self._human is None:
            return False
        try:
            since = self._human.seconds_since_last_real_input()
        except Exception:
            return False
        return since is not None and since < self._cfg.human_grace_seconds

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._enabled:
                self._stop.wait(0.5)
                continue
            now = time.perf_counter()
            elapsed = now - self._last_send_at
            if elapsed < self._cfg.interval_seconds:
                # Sleep until either the interval expires or we get stopped.
                remaining = self._cfg.interval_seconds - elapsed
                self._stop.wait(min(remaining, 1.0))
                continue
            if self._human_input_recent():
                # Real human is at the keyboard — they aren't idle.
                self._stop.wait(2.0)
                continue
            try:
                ok = self.fire_once()
                self._log(
                    f"[anti-idle] sent {self._cfg.key!r} (success={ok}, "
                    f"#sends={self._sends})"
                )
            except Exception as e:
                self._log(f"[anti-idle] send failed: {e}")
                self._stop.wait(2.0)
