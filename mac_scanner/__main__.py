"""Entry point: `python -m mac_scanner`.

Wires the capture loop, OCR engine, matcher, notifier, input monitor,
and (optionally) the UI together. Worker threads do the heavy lifting;
the main thread runs either the Tk event loop or, in headless mode, a
plain sleep loop until Ctrl+C.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import datetime

import cv2
from rapidfuzz import fuzz

from . import capture, config
from .input_monitor import HumanInputMonitor
from .input_sender import (
    AntiIdleConfig,
    AntiIdleSender,
    AutoGrabConfig,
    AutoGrabber,
    activate_remote_play,
    auto_grab,
    frontmost_application,
    is_accessibility_trusted,
    send_keypress,
)
from .matcher import find_spawn_event, parse_with_trace
from .notify import Notifier, TelegramBot, macos_notify
from .ocr import OCREngine, get_engine
from .targets import TargetStore, normalize
from .ui import ControlApp


LOG_PATH = Path(__file__).resolve().parent.parent / "mac_scanner.log"


def _setup_file_logger() -> logging.Logger:
    logger = logging.getLogger("mac_scanner")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


class ScannerApp:
    def __init__(
        self,
        *,
        headless: bool = False,
        calibration: bool = False,
        anti_idle: bool = False,
        anti_idle_interval: float | None = None,
        auto_grab_on_hit: bool = False,
    ) -> None:
        self._headless = headless
        self._logger = _setup_file_logger()
        self._logger.info("=" * 60)
        self._logger.info(
            "mac_scanner starting up (mode=%s, log: %s)",
            "headless" if headless else "gui",
            LOG_PATH,
        )

        self._store = TargetStore()
        loaded = self._store.load_collected()
        self._logger.info(
            "targets active: %s",
            {t: {r: len(n) for r, n in v.items()} for t, v in self._store.snapshot().items()},
        )

        self._engine: OCREngine | None = None
        self._engine_error: str | None = None

        self._telegram = TelegramBot(self._store, on_log=self._log)
        self._notifier = Notifier(self._store, telegram=self._telegram, on_log=self._log)

        self._input_monitor = HumanInputMonitor()

        anti_idle_enabled = anti_idle or config.ANTI_IDLE_ENABLED
        anti_idle_cfg = AntiIdleConfig.from_config_module()
        if anti_idle_interval is not None:
            anti_idle_cfg = AntiIdleConfig(
                key=anti_idle_cfg.key,
                interval_seconds=float(anti_idle_interval),
                focus_remote_play=anti_idle_cfg.focus_remote_play,
                hold_seconds=anti_idle_cfg.hold_seconds,
                human_grace_seconds=anti_idle_cfg.human_grace_seconds,
            )
        self._anti_idle = AntiIdleSender(
            anti_idle_cfg,
            human_monitor=self._input_monitor,
            on_log=self._log,
        )
        self._anti_idle_initial_enabled = anti_idle_enabled

        auto_grab_cfg = AutoGrabConfig.from_config_module()
        self._auto_grabber = AutoGrabber(
            auto_grab_cfg,
            human_monitor=self._input_monitor,
            on_log=self._log,
        )
        self._auto_grab_on_hit: bool = (
            auto_grab_on_hit or config.AUTO_GRAB_ON_HIT_ENABLED
        )

        self._region: capture.Region | None = None
        self._region_source = "none"
        self._region_lock = threading.Lock()

        self._running = False
        self._calibration = calibration
        self._stop_workers = threading.Event()
        self._last_hit_at = 0.0
        self._last_scan_summary: str | None = None
        self._last_scan_log_at: float = 0.0
        self._last_debug_dump_at: float = 0.0
        self._known_card_names: list[str] = sorted(config.ALL_CARD_NAMES)

        self._grabber: capture.ScreenGrabber | None = None
        self._capture_thread: threading.Thread | None = None

        self._caffeinate_proc: subprocess.Popen[bytes] | None = None

        self._app: ControlApp | None
        if headless:
            self._app = None
        else:
            self._app = ControlApp(
                on_running_changed=self._handle_running_changed,
                on_calibration_changed=self._handle_calibration_changed,
                on_telegram_changed=self._handle_telegram_changed,
                on_pause_input_changed=self._handle_pause_input_changed,
                on_recompute_region=self._refresh_region,
                on_save_region_override=self._save_region_override,
                telegram_available=self._telegram.enabled,
            )

        if loaded:
            self._log(f"[init] loaded {loaded} previously-collected target(s)")
        self._log(
            f"[init] OCR engine selected: {config.OCR_ENGINE} "
            f"(image scale: {config.OCR_IMAGE_SCALE}x, "
            f"trim borders: {config.OCR_TRIM_BORDERS})"
        )
        if self._telegram.enabled:
            self._log("[init] Telegram credentials present, polling enabled")
        else:
            self._log("[init] Telegram disabled (set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID to enable)")
        if config.DEBUG_DUMP:
            self._log(
                f"[init] DEBUG DUMP ON — saving frames + OCR to "
                f"{config.DEBUG_DUMP_DIR} on missed-label scenarios"
            )
        if headless:
            self._log("[init] headless mode — scanner will start immediately, Ctrl+C to quit")
            if calibration:
                self._log("[init] calibration logging enabled")
        if anti_idle_enabled:
            self._log(
                f"[init] anti-idle armed — will send {anti_idle_cfg.key!r} every "
                f"{anti_idle_cfg.interval_seconds:.0f}s "
                f"(focus_remote_play={anti_idle_cfg.focus_remote_play})"
            )
        if self._auto_grab_on_hit:
            self._log(
                f"[init] auto-grab-on-hit armed — on every card hit, will "
                f"spam {auto_grab_cfg.key!r} for "
                f"{auto_grab_cfg.duration_seconds:.1f}s "
                f"(delay={auto_grab_cfg.delay_seconds:.2f}s, ~"
                f"{int(auto_grab_cfg.duration_seconds / max(auto_grab_cfg.delay_seconds, 0.001))} presses)"
            )

    # ── infrastructure ──

    def _log(self, message: str, *, file_only: bool = False) -> None:
        self._logger.info(message)
        if file_only:
            return
        if self._app is not None:
            try:
                self._app.log(message)
                return
            except Exception:
                pass
        # Headless (or UI errored): mirror to stdout with a timestamp.
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{ts} {message}", flush=True)

    def _ensure_engine(self) -> OCREngine | None:
        if self._engine is not None or self._engine_error is not None:
            return self._engine
        try:
            custom_words = self._build_custom_words()
            self._engine = get_engine(custom_words=custom_words)
            self._log(
                f"[ocr] engine ready: {self._engine.name} "
                f"(customWords: {len(custom_words)})"
            )
        except Exception as e:
            self._engine_error = str(e)
            self._log(f"[ocr] failed to load engine '{config.OCR_ENGINE}': {e}")
            if config.ENABLE_MACOS_TOAST:
                macos_notify(
                    "Card Scanner",
                    "OCR unavailable",
                    f"Could not load {config.OCR_ENGINE}: {e}",
                )
        return self._engine

    def _build_custom_words(self) -> tuple[str, ...]:
        """All vocabulary tokens we'd like Vision to bias toward."""
        words: set[str] = set()
        for name in config.ALL_CARD_NAMES:
            words.add(name)
            for token in name.split():
                if token:
                    words.add(token)
        words.update(config.TIERS)
        words.update(config.RARITIES)
        return tuple(sorted(words))

    def _ensure_grabber(self) -> capture.ScreenGrabber | None:
        if self._grabber is not None:
            return self._grabber
        try:
            self._grabber = capture.ScreenGrabber()
        except Exception as e:
            self._log(f"[capture] failed to init mss: {e}")
        return self._grabber

    def _refresh_region(self) -> None:
        region, source = capture.resolve_region(prefer_override=True)
        with self._region_lock:
            self._region = region
            self._region_source = source
        if self._app is not None:
            self._app.update_region_label(region, source)
        if region is None:
            hint = (
                "Open it, then click Recompute."
                if self._app is not None else
                "Open it; the scanner will keep retrying every ~0.5s."
            )
            self._log(f"[region] PS Remote Play window not found. {hint}")
        else:
            self._log(f"[region] {source}: {region.width}x{region.height} @ ({region.left},{region.top})")

    def _save_region_override(self) -> None:
        with self._region_lock:
            region = self._region
        if region is None:
            self._log("[region] nothing to save (no region resolved)")
            return
        try:
            capture.save_region_override(region)
            self._log(f"[region] saved override to {config.REGION_OVERRIDE_FILE}")
        except Exception as e:
            self._log(f"[region] failed to save override: {e}")

    # ── UI handlers ──

    def _handle_running_changed(self, running: bool) -> None:
        self._running = running
        if running:
            self._refresh_region()
            with self._region_lock:
                if self._region is None:
                    self._log("[run] no region resolved yet — start will idle until found")
                elif self._app is not None:
                    self._app.show_overlay(self._region)
            if self._ensure_engine() is None:
                self._log("[run] OCR engine unavailable; running will produce no detections")
        elif self._app is not None:
            self._app.hide_overlay()

    def _handle_calibration_changed(self, on: bool) -> None:
        self._calibration = on
        self._log(f"[calib] {'on' if on else 'off'}")

    def _handle_telegram_changed(self, on: bool) -> None:
        self._notifier.telegram_enabled = on and self._telegram.enabled
        self._log(f"[telegram] {'enabled' if self._notifier.telegram_enabled else 'disabled'}")

    def _handle_pause_input_changed(self, on: bool) -> None:
        self._input_monitor.set_enabled(on)
        self._log(f"[pause-on-input] {'enabled' if on else 'disabled'}")

    # ── capture/OCR loop ──

    def _capture_loop(self) -> None:
        while not self._stop_workers.is_set():
            if not self._running:
                self._stop_workers.wait(0.1); continue
            if self._input_monitor.is_paused:
                self._stop_workers.wait(0.05); continue
            if time.perf_counter() - self._last_hit_at < config.COOLDOWN_AFTER_HIT_SECONDS:
                self._stop_workers.wait(0.05); continue
            if self._auto_grabber.is_active:
                # An auto-grab burst is still spamming the interact key.
                # Skip OCR so we don't re-detect the same card and queue
                # a second burst the instant this one finishes.
                self._stop_workers.wait(0.1); continue

            with self._region_lock:
                region = self._region
            if region is None:
                self._stop_workers.wait(0.5); continue

            engine = self._ensure_engine()
            grabber = self._ensure_grabber()
            if engine is None or grabber is None:
                self._stop_workers.wait(0.5); continue

            t0 = time.perf_counter()
            try:
                img = grabber.grab_bgr(region)
                img_for_ocr = capture.preprocess_for_ocr(
                    img,
                    scale=config.OCR_IMAGE_SCALE,
                    trim_borders=config.OCR_TRIM_BORDERS,
                )
                results = engine.recognize(img_for_ocr)
            except Exception as e:
                self._log(f"[capture] error: {e}")
                self._stop_workers.wait(0.3); continue

            if self._calibration:
                self._dump_ocr(results, t0)

            spawn = find_spawn_event(results)
            if spawn is not None and self._store.has_bucket(spawn.tier, spawn.rarity):
                self._log(
                    f"[spawn] {spawn.tier}/{spawn.rarity} @ {spawn.location} "
                    f"raw={spawn.raw!r}"
                )
                self._notifier.spawn_detected(spawn.tier, spawn.rarity, spawn.location)
                if config.ENABLE_SPAWN_NOTIFICATIONS:
                    self._last_hit_at = time.perf_counter()

            trace = parse_with_trace(results, self._store)
            self._maybe_log_scan(trace, results, t0)
            if config.DEBUG_DUMP:
                self._maybe_dump_debug_frame(img_for_ocr, results, trace)

            fired = False
            seen_in_frame: set[tuple[str, str, str]] = set()
            first_hit_label: str | None = None
            for match in sorted(trace.matches, key=lambda m: -m.score):
                key = (match.tier, match.rarity, match.name)
                if key in seen_in_frame:
                    continue
                seen_in_frame.add(key)
                if not self._store.is_active(*key):
                    continue
                self._log(
                    f"[hit] {match.name} ({match.tier}/{match.rarity}) "
                    f"score={match.score:.0f} ocr={time.perf_counter()-t0:.2f}s"
                )
                self._notifier.card_detected(match.tier, match.rarity, match.name)
                if first_hit_label is None:
                    first_hit_label = f"{match.tier}/{match.rarity}/{match.name}"
                fired = True
            if fired:
                self._last_hit_at = time.perf_counter()
                if self._auto_grab_on_hit and not self._auto_grabber.is_active:
                    self._auto_grabber.grab_async(label=f"for {first_hit_label}")

            elapsed = time.perf_counter() - t0
            self._stop_workers.wait(max(0.0, config.CAPTURE_INTERVAL_SECONDS - elapsed))

    def _maybe_log_scan(self, trace, results, t0: float) -> None:
        """Log a [scan] diagnostic line when something interesting changed.

        Always written to the file log; mirrored to the UI only when the
        summary changes (so a stationary screen doesn't spam the panel).
        """
        any_signal = bool(trace.tiers_seen or trace.rarities_seen or trace.candidate_texts)
        if not any_signal:
            return
        summary = trace.as_log_str()
        msg = f"[scan] {time.perf_counter()-t0:.2f}s ocr_strings={len(results)} {summary}"
        self._logger.info(msg)
        now = time.perf_counter()
        if summary != self._last_scan_summary or now - self._last_scan_log_at > 5.0:
            self._last_scan_summary = summary
            self._last_scan_log_at = now
            if self._app is not None:
                try:
                    self._app.log(msg)
                except Exception:
                    pass

    def _maybe_dump_debug_frame(self, img, results, trace) -> None:
        """Save a frame + OCR readings to disk when something looks like
        a card name but no tier/rarity label was extracted. This is the
        diagnostic hook for "I saw a rare droid but got no notification"
        situations: it captures exactly what the OCR layer saw at that
        moment so we can tell whether the labels were missed by Vision
        or never visible at all."""
        if trace.tiers_seen and trace.rarities_seen:
            return
        if not trace.candidate_texts:
            return

        likely_card_text: str | None = None
        likely_card_score: float = 0.0
        likely_card_match: str | None = None
        for text in trace.candidate_texts:
            for name in self._known_card_names:
                score = float(fuzz.WRatio(text, name))
                if score >= 85.0 and score > likely_card_score:
                    likely_card_text = text
                    likely_card_score = score
                    likely_card_match = name
        if likely_card_text is None:
            return

        now = time.perf_counter()
        if now - self._last_debug_dump_at < config.DEBUG_DUMP_MIN_INTERVAL_SECONDS:
            return
        self._last_debug_dump_at = now

        try:
            config.DEBUG_DUMP_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            png_path = config.DEBUG_DUMP_DIR / f"{ts}.png"
            txt_path = config.DEBUG_DUMP_DIR / f"{ts}.txt"
            cv2.imwrite(str(png_path), img)
            with txt_path.open("w", encoding="utf-8") as f:
                f.write(f"Captured: {datetime.datetime.now().isoformat()}\n")
                f.write(f"Image size: {img.shape[1]}x{img.shape[0]}\n")
                f.write(
                    f"Likely card name candidate: {likely_card_text!r} -> "
                    f"{likely_card_match!r} (WRatio={likely_card_score:.1f})\n"
                )
                f.write(f"Trace: {trace.as_log_str()}\n")
                f.write(f"OCR strings ({len(results)}):\n")
                for r in results:
                    if r.bbox is not None:
                        bbox = (
                            f" [{r.bbox[0]:.0f},{r.bbox[1]:.0f}"
                            f" {r.bbox[2]:.0f}x{r.bbox[3]:.0f}]"
                        )
                    else:
                        bbox = ""
                    f.write(f"  {r.confidence:5.2f}  {r.text!r}{bbox}\n")
            self._log(
                f"[debug-dump] saved {png_path.name}: "
                f"saw {likely_card_text!r}~{likely_card_match} "
                f"(score={likely_card_score:.0f}) but no rarity label"
            )
        except Exception as e:
            self._log(f"[debug-dump] failed: {e}", file_only=True)

    def _dump_ocr(self, results, t0: float) -> None:
        if not results:
            self._log(f"[calib] no text detected ({time.perf_counter()-t0:.2f}s)")
            return
        head = ", ".join(f"{normalize(r.text)!r}@{r.confidence:.2f}" for r in results[:12])
        more = "" if len(results) <= 12 else f" (+{len(results)-12} more)"
        self._log(f"[calib] {time.perf_counter()-t0:.2f}s | {head}{more}")

    # ── lifecycle ──

    def run(self) -> None:
        if sys.platform == "darwin" and config.CAFFEINATE_DISPLAY:
            caffeine = "/usr/bin/caffeinate"
            try:
                self._caffeinate_proc = subprocess.Popen(
                    [caffeine, "-d", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._log("[caffeinate] display sleep suppressed (-d) while scanner runs")
            except OSError as e:
                self._log(f"[caffeinate] could not start: {e}")

        self._input_monitor.start()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        self._anti_idle.start()
        if self._anti_idle_initial_enabled:
            self._anti_idle.set_enabled(True)
        if self._telegram.enabled:
            self._telegram.start_polling()

        self._refresh_region()

        try:
            if self._app is not None:
                self._app.mainloop()
            else:
                self._run_headless()
        finally:
            self._stop_workers.set()
            self._anti_idle.stop()
            self._telegram.stop()
            self._input_monitor.stop()
            if self._grabber is not None:
                self._grabber.close()
            proc = self._caffeinate_proc
            self._caffeinate_proc = None
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=2.0)
                except Exception:
                    pass

    def _run_headless(self) -> None:
        """Auto-start the scanner and block until SIGINT/SIGTERM."""
        # Eagerly load the OCR engine so any failure is reported up front
        # rather than silently producing zero detections.
        if self._ensure_engine() is None:
            self._log("[run] OCR engine unavailable; capture loop will idle")
        self._running = True
        self._log("[run] scanning started (headless)")

        def _on_signal(signum, _frame):
            self._log(f"[run] received signal {signum}, shutting down")
            self._stop_workers.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                # signal() can fail on non-main threads, but we're on
                # the main thread here so this is mostly defensive.
                pass

        try:
            while not self._stop_workers.wait(0.5):
                pass
        except KeyboardInterrupt:
            self._log("[run] keyboard interrupt, shutting down")
            self._stop_workers.set()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m mac_scanner",
        description=(
            "Card scanner for the Droid Tycoon UEFN island, driven from PS "
            "Remote Play frames on macOS."
        ),
    )
    p.add_argument(
        "--headless", "--no-gui",
        dest="headless",
        action="store_true",
        help=(
            "Run without the Tk control window. The scanner starts "
            "immediately, logs to stdout + mac_scanner.log, and quits on "
            "Ctrl+C / SIGTERM."
        ),
    )
    p.add_argument(
        "--calibration",
        action="store_true",
        help=(
            "Start with calibration logging on (every OCR string is "
            "dumped). Useful with --headless for one-off diagnostics."
        ),
    )
    p.add_argument(
        "--no-caffeinate",
        action="store_true",
        help=(
            "Do not run caffeinate -d (allow the display to sleep per "
            "Energy settings). Default is on; disable globally with "
            "MAC_SCANNER_CAFFEINATE=0."
        ),
    )
    p.add_argument(
        "--anti-idle",
        action="store_true",
        help=(
            "Periodically synthesize a spacebar press to PS Remote Play "
            "so the in-game character jumps and the UEFN island doesn't "
            "kick you for being idle. Disabled by default. Interval "
            "defaults to MAC_SCANNER_ANTI_IDLE_INTERVAL (300s = 5 min). "
            "Requires Accessibility permission for the parent process."
        ),
    )
    p.add_argument(
        "--anti-idle-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Override how often the anti-idle key fires (default: "
            "config.ANTI_IDLE_INTERVAL_SECONDS, 300s = 5 min)."
        ),
    )
    p.add_argument(
        "--send-space",
        action="store_true",
        help=(
            "One-shot: focus PS Remote Play, synthesize ONE spacebar "
            "press, and exit. Use this to verify Accessibility "
            "permission and that the key is reaching the game."
        ),
    )
    p.add_argument(
        "--send-key",
        type=str,
        default=None,
        metavar="KEY",
        help=(
            "Like --send-space but for an arbitrary key name (see "
            "input_sender.KEY_CODES). Mutually exclusive with --send-space."
        ),
    )
    p.add_argument(
        "--send-count",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Repeat --send-key/--send-space N times with --send-delay "
            "between each. Useful for visually confirming the game is "
            "receiving the keystroke (e.g. several jumps in a row)."
        ),
    )
    p.add_argument(
        "--send-delay",
        type=float,
        default=0.75,
        metavar="SECONDS",
        help="Delay between repeats when --send-count > 1 (default 0.75s).",
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help=(
            "Run all anti-idle preflight checks and print exact "
            "remediation steps: Accessibility permission, Remote Play "
            "window discovery, focus activation, and a verified "
            "keystroke. Run this FIRST if --send-space appears to "
            "succeed but the in-game character doesn't move."
        ),
    )
    p.add_argument(
        "--auto-grab",
        action="store_true",
        help=(
            "One-shot: focus PS Remote Play, spam the configured "
            "auto-grab key (default 'E', for the in-game pickup/"
            "interact action) for MAC_SCANNER_AUTO_GRAB_DURATION "
            "seconds at MAC_SCANNER_AUTO_GRAB_DELAY-second intervals, "
            "then exit. Disabled by default; use this to test or to "
            "force a manual grab burst."
        ),
    )
    p.add_argument(
        "--auto-grab-on-hit",
        action="store_true",
        help=(
            "Run normally, but EVERY card hit automatically triggers "
            "an auto-grab burst — spam the interact key for 3 s at "
            "0.1 s intervals (configurable via MAC_SCANNER_AUTO_GRAB_* "
            "env vars). Use with care: this turns the scanner from "
            "notify-only into an automated collector."
        ),
    )
    p.add_argument(
        "--auto-grab-duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Override the total auto-grab burst length (default 3.0). "
            "Affects both --auto-grab one-shots and --auto-grab-on-hit."
        ),
    )
    p.add_argument(
        "--auto-grab-delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Override the per-press delay inside an auto-grab burst "
            "(default 0.1)."
        ),
    )
    p.add_argument(
        "--auto-grab-key",
        type=str,
        default=None,
        metavar="KEY",
        help=(
            "Override the key spammed by auto-grab (default 'e'). "
            "Must be in input_sender.KEY_CODES."
        ),
    )
    return p.parse_args(argv)


def _run_send_key_oneshot(key: str, count: int = 1, delay: float = 0.75) -> int:
    """Activate Remote Play, post key(s), exit. Smoke test path."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ok_any = False
    try:
        for i in range(max(1, count)):
            ok = send_keypress(
                key,
                focus_remote_play=config.ANTI_IDLE_FOCUS_REMOTE_PLAY,
                hold_seconds=config.ANTI_IDLE_KEY_HOLD_SECONDS,
            )
            ok_any = ok_any or ok
            print(f"send_keypress({key!r}) [{i+1}/{count}] returned {ok}")
            if i + 1 < count:
                time.sleep(delay)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0 if ok_any else 1


def _run_auto_grab_oneshot(
    key: str | None,
    duration: float | None,
    delay: float | None,
) -> int:
    """One-shot CLI path for `--auto-grab`. Spam, then exit."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AutoGrabConfig.from_config_module()
    cfg = AutoGrabConfig(
        key=(key or cfg.key).lower(),
        duration_seconds=cfg.duration_seconds if duration is None else float(duration),
        delay_seconds=cfg.delay_seconds if delay is None else float(delay),
        hold_seconds=cfg.hold_seconds,
        focus_remote_play=cfg.focus_remote_play,
    )
    try:
        count = auto_grab(
            cfg.key,
            duration_seconds=cfg.duration_seconds,
            delay_seconds=cfg.delay_seconds,
            hold_seconds=cfg.hold_seconds,
            focus_remote_play=cfg.focus_remote_play,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(
        f"auto_grab({cfg.key!r}, duration={cfg.duration_seconds}s, "
        f"delay={cfg.delay_seconds}s) fired {count} presses"
    )
    return 0 if count > 0 else 1


def _run_diagnose() -> int:
    """Walk every anti-idle prerequisite and print actionable output.

    Order matters: cheap, local conditions first (process trust, Quartz
    import) before anything that affects the user's UI (activating
    Remote Play, posting a key).

    On macOS Sonoma+ a non-frontmost terminal CANNOT pull focus to
    another app — `activateWithOptions_` returns True but the OS keeps
    your Terminal frontmost. That is the *expected* OS behavior, NOT a
    misconfiguration. The diagnostic treats "Remote Play already
    frontmost" as the green-light condition, and warns (but does not
    fail) when focus-stealing is blocked because that case still works
    in production — the user keeps Remote Play focused while AFK and
    the anti-idle press lands every time.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    issues: list[str] = []
    notes: list[str] = []
    print("─── mac_scanner anti-idle diagnostics ───")
    print(f"  python   : {sys.executable}")
    print(f"  platform : {sys.platform}")

    trusted = is_accessibility_trusted()
    print(f"  AXIsProcessTrusted: {trusted}")
    if not trusted:
        issues.append(
            "Accessibility is NOT granted. CGEventPost will silently drop "
            "every key event. Fix:\n"
            f"    1. Open System Settings → Privacy & Security → Accessibility.\n"
            f"    2. Click '+' and add: {sys.executable}\n"
            "       (or add your Terminal / iTerm / Cursor app instead — child\n"
            "        processes inherit the grant).\n"
            "    3. Toggle the entry ON. If it was already there, REMOVE and re-add\n"
            "       it (macOS pins by file inode, which changes when the venv is\n"
            "       recreated).\n"
            "    4. Quit the terminal completely (⌘Q) and reopen, then rerun this."
        )

    try:
        import Quartz  # type: ignore[import]  # noqa: F401
        print("  Quartz import: ok")
    except ImportError as e:
        print(f"  Quartz import: FAIL ({e})")
        issues.append(
            "PyObjC Quartz is not importable. Run "
            "`pip install -r requirements_mac.txt` inside the venv."
        )

    from . import capture
    region = capture.find_remote_play_window()
    print(f"  Remote Play window: {region}")
    if region is None:
        issues.append(
            "PS Remote Play window not found by CGWindowList. Open the "
            "Remote Play app, connect to your PS5, and make sure the "
            "window is on-screen (not minimized / hidden behind Stage "
            "Manager)."
        )

    if region is not None:
        before = frontmost_application()
        before_name = before["name"] if before else None
        wanted_lower = {n.lower() for n in config.REMOTE_PLAY_OWNER_NAMES}
        already_frontmost = (
            before is not None and before_name.lower() in wanted_lower
        )
        print(f"  frontmost (before activate): {before}")
        if already_frontmost:
            print(
                "  activation test: SKIPPED — Remote Play is already "
                "frontmost. The anti-idle press will land directly; "
                "no focus transition needed."
            )
        else:
            activated = activate_remote_play()
            after = frontmost_application()
            print(f"  activate_remote_play: {activated}")
            print(f"  frontmost (after activate):  {after}")
            if not activated:
                # The most common cause on Sonoma+ is focus-stealing
                # prevention from a foreground Terminal. That's a
                # USER-LEVEL constraint, not a code bug.
                notes.append(
                    "macOS prevented this Terminal/IDE from pulling focus to "
                    "PS Remote Play. This is normal in Sonoma/Sequoia: an app "
                    "in the background cannot steal focus from your foreground "
                    "app. For anti-idle to work in production:\n"
                    "    * Click PS Remote Play once so it's the frontmost window,\n"
                    "      THEN switch back to the terminal and start --anti-idle.\n"
                    "      Once Remote Play has focus, the anti-idle loop keeps\n"
                    "      it that way.\n"
                    "    * The OCR side of the scanner already requires Remote\n"
                    "      Play to be visible, so this is the normal setup anyway.\n"
                    "    * (Optional, for AppleScript fallback to work too) System\n"
                    "      Settings → Privacy & Security → Automation → enable\n"
                    "      PS Remote Play under your terminal/IDE entry."
                )

    if not issues:
        print(
            "\nAll required preflight checks passed. Posting a single "
            "spacebar now — watch the in-game character. If it doesn't "
            "jump, the most likely remaining cause is that PS Remote "
            "Play's keyboard input is disabled or its space-key map "
            "isn't bound to the X (Cross) button. See the 'Anti-idle' "
            "section of README.md."
        )
        send_keypress(
            "space",
            focus_remote_play=config.ANTI_IDLE_FOCUS_REMOTE_PLAY,
            hold_seconds=config.ANTI_IDLE_KEY_HOLD_SECONDS,
        )

    print("\n─── result ───")
    if not issues and not notes:
        print("OK — no preflight issues detected.")
        return 0
    if not issues:
        print("OK with notes — anti-idle will work in production:")
        for i, msg in enumerate(notes, 1):
            print(f"\n  [note {i}] {msg}")
        return 0
    for i, msg in enumerate(issues, 1):
        print(f"\n[{i}] {msg}")
    for i, msg in enumerate(notes, 1):
        print(f"\n[note {i}] {msg}")
    return 1


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "darwin":
        print("mac_scanner is macOS-only. Use card_scanner.py on Windows.", file=sys.stderr)
        return 2
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.diagnose:
        return _run_diagnose()
    if args.send_space and args.send_key:
        print("error: --send-space and --send-key are mutually exclusive", file=sys.stderr)
        return 2
    if args.send_space:
        return _run_send_key_oneshot("space", count=args.send_count, delay=args.send_delay)
    if args.send_key is not None:
        return _run_send_key_oneshot(args.send_key, count=args.send_count, delay=args.send_delay)
    if args.auto_grab:
        return _run_auto_grab_oneshot(
            key=args.auto_grab_key,
            duration=args.auto_grab_duration,
            delay=args.auto_grab_delay,
        )

    # Apply optional auto-grab overrides into the config module so the
    # ScannerApp picks them up when it constructs AutoGrabConfig.
    if args.auto_grab_key is not None:
        config.AUTO_GRAB_KEY = args.auto_grab_key.lower()
    if args.auto_grab_duration is not None:
        config.AUTO_GRAB_DURATION_SECONDS = float(args.auto_grab_duration)
    if args.auto_grab_delay is not None:
        config.AUTO_GRAB_DELAY_SECONDS = float(args.auto_grab_delay)

    if args.no_caffeinate:
        config.CAFFEINATE_DISPLAY = False

    ScannerApp(
        headless=args.headless,
        calibration=args.calibration,
        anti_idle=args.anti_idle,
        anti_idle_interval=args.anti_idle_interval,
        auto_grab_on_hit=args.auto_grab_on_hit,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
