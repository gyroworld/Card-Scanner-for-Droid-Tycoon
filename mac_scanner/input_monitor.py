"""Pause-on-human-input monitor.

We never synthesize input, but pausing OCR while the user is actively
typing or moving the mouse can keep CPU load down during real
interaction. This is a soft, opt-in feature — it ships disabled and
must be turned on in the UI checkbox. Toggling it has no effect on
correctness, only on capture cadence while you're using the keyboard
or mouse.
"""

from __future__ import annotations

import threading
import time

from pynput import keyboard as pkeyboard, mouse as pmouse


class HumanInputMonitor:
    """Sets `is_paused` True for `quiet_after` seconds after any input."""

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

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._is_paused and self._enabled

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._enabled = on
            if not on:
                self._is_paused = False

    def _touch(self) -> None:
        with self._lock:
            self._last_input_at = time.perf_counter()
            self._is_paused = True

    def _on_move(self, _x, _y) -> None:
        self._touch()

    def _on_click(self, _x, _y, _button, pressed) -> None:
        if pressed:
            self._touch()

    def _on_key(self, _key) -> None:
        self._touch()

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
