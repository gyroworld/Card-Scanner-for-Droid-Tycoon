"""Pause-on-human-input monitor.

The capture loop uses this to optionally back off while the user is
actively typing or moving the mouse. The anti-idle sender uses it to
check whether the user has been active recently (and therefore doesn't
need a synthetic keypress).

We never pause for our OWN synthetic key events: the anti-idle module
calls `mark_synth_window(duration)` right before posting a key, and
key events landing inside that window are ignored. Mouse events are
never produced by us, so they always count.
"""

from __future__ import annotations

import threading
import time

from pynput import keyboard as pkeyboard, mouse as pmouse


class HumanInputMonitor:
    """Tracks recent real human input.

    Public state:
      * `is_paused` — True for `quiet_after` seconds after any real
        input, *and* only when `set_enabled(True)` was called.
        Capture loop checks this to decide whether to skip a frame.
      * `seconds_since_last_real_input()` — None until we've seen any
        input, otherwise wall-clock seconds since the last touch.
        Used by the anti-idle sender to skip while you're really
        playing.
    """

    def __init__(self, quiet_after: float = 1.0) -> None:
        self._quiet_after = quiet_after
        self._last_input_at: float = 0.0
        self._is_paused = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._mouse: pmouse.Listener | None = None
        self._kbd: pkeyboard.Listener | None = None
        self._monitor: threading.Thread | None = None
        # Default OFF: typing or moving the mouse should NOT pause OCR
        # unless the user explicitly enables the toggle in the UI.
        self._enabled = False
        # Time until which our own synthesized key events should be
        # ignored. The anti-idle sender bumps this before each post.
        self._synth_until: float = 0.0

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._is_paused and self._enabled

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._enabled = on
            if not on:
                self._is_paused = False

    def mark_synth_window(self, duration: float) -> None:
        """Ignore key events for the next `duration` seconds.

        The anti-idle sender calls this immediately before posting a
        synthetic key, because pynput's event tap sees the key just
        like a real keystroke would.
        """
        with self._lock:
            self._synth_until = max(
                self._synth_until, time.perf_counter() + duration
            )

    def seconds_since_last_real_input(self) -> float | None:
        with self._lock:
            if self._last_input_at == 0.0:
                return None
            return time.perf_counter() - self._last_input_at

    def _touch_key(self) -> None:
        with self._lock:
            if time.perf_counter() < self._synth_until:
                return
            self._last_input_at = time.perf_counter()
            self._is_paused = True

    def _touch_mouse(self) -> None:
        with self._lock:
            self._last_input_at = time.perf_counter()
            self._is_paused = True

    def _on_move(self, _x, _y) -> None:
        self._touch_mouse()

    def _on_click(self, _x, _y, _button, pressed) -> None:
        if pressed:
            self._touch_mouse()

    def _on_key(self, _key) -> None:
        self._touch_key()

    def _resume_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                paused = self._is_paused
                last = self._last_input_at
            if paused and (time.perf_counter() - last) > self._quiet_after:
                with self._lock:
                    self._is_paused = False
            self._stop.wait(0.2)

    def start(self) -> None:
        self._mouse = pmouse.Listener(on_move=self._on_move, on_click=self._on_click)
        self._mouse.daemon = True
        self._mouse.start()

        self._kbd = pkeyboard.Listener(on_press=self._on_key)
        self._kbd.daemon = True
        self._kbd.start()

        self._monitor = threading.Thread(target=self._resume_loop, daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        for listener in (self._mouse, self._kbd):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
