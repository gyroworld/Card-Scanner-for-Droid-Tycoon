"""Tkinter UI: a normal control window plus a 4-bar outline overlay.

The Windows version of this scanner used Tk's `-transparentcolor` chroma
key to create a hollow always-on-top rectangle so OCR could see through
the overlay. That attribute does not exist on macOS, so we instead build
the rectangle out of four separate thin frameless windows positioned at
the edges of the capture region. Their interior is genuinely empty — no
window at all sits over the cards, so OCR is unaffected.

We also try to make the bars click-through via NSWindow.ignoresMouseEvents
(PyObjC). If the lookup fails the bars are still thin enough not to
matter much.
"""

from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

from . import capture


# ─── helpers ─────────────────────────────────────────────────────────────────

def _backing_scale() -> float:
    try:
        from AppKit import NSScreen  # type: ignore[import]
        s = NSScreen.mainScreen()
        return float(s.backingScaleFactor()) if s else 1.0
    except Exception:
        return 2.0


def _set_click_through(top: tk.Toplevel) -> None:
    """Best-effort: make this Toplevel's underlying NSWindow ignore mouse events."""
    try:
        from AppKit import NSApp  # type: ignore[import]
        top.update_idletasks()
        unique = f"_mac_scanner_overlay_{id(top)}"
        top.title(unique)
        for w in NSApp.windows():
            if w.title() == unique:
                w.setIgnoresMouseEvents_(True)
                # Make sure it floats above everything, including fullscreen.
                # NSStatusWindowLevel = 25; well above normal/floating.
                try:
                    w.setLevel_(25)
                except Exception:
                    pass
                break
    except Exception:
        pass


# ─── outline overlay ─────────────────────────────────────────────────────────

class OutlineOverlay:
    """Four thin frameless Tk windows forming a rectangle around a region."""

    THICKNESS = 3
    COLOR = "#00FF88"

    def __init__(self, parent: tk.Misc) -> None:
        self._parent = parent
        self._bars: list[tk.Toplevel] = []
        self._scale = _backing_scale() or 1.0
        self._visible = False

    def _ensure_bars(self) -> None:
        if self._bars:
            return
        for _ in range(4):
            t = tk.Toplevel(self._parent)
            t.overrideredirect(True)
            t.configure(bg=self.COLOR)
            try:
                t.attributes("-topmost", True)
            except tk.TclError:
                pass
            t.withdraw()
            self._bars.append(t)
            _set_click_through(t)

    def show(self, region: capture.Region) -> None:
        self._ensure_bars()
        s = self._scale
        # Convert physical pixels back to logical points for Tk geometry.
        x = int(region.left / s)
        y = int(region.top / s)
        w = int(region.width / s)
        h = int(region.height / s)
        t = self.THICKNESS
        rects = [
            (x, y, w, t),                 # top
            (x, y + h - t, w, t),         # bottom
            (x, y, t, h),                 # left
            (x + w - t, y, t, h),         # right
        ]
        for bar, (bx, by, bw, bh) in zip(self._bars, rects):
            bar.geometry(f"{max(bw,1)}x{max(bh,1)}+{bx}+{by}")
            bar.deiconify()
            try:
                bar.lift()
            except tk.TclError:
                pass
        self._visible = True

    def hide(self) -> None:
        for bar in self._bars:
            try:
                bar.withdraw()
            except tk.TclError:
                pass
        self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible


# ─── control window ──────────────────────────────────────────────────────────

class ControlApp(tk.Tk):
    """Main control window. Hosts the log, toggles, status, and overlay."""

    BG = "#101418"
    FG = "#DDDDDD"
    ACCENT = "#00FF88"
    ALERT = "#FF4444"

    def __init__(
        self,
        *,
        on_running_changed: Callable[[bool], None],
        on_calibration_changed: Callable[[bool], None],
        on_telegram_changed: Callable[[bool], None],
        on_pause_input_changed: Callable[[bool], None],
        on_recompute_region: Callable[[], None],
        on_save_region_override: Callable[[], None],
        telegram_available: bool,
    ) -> None:
        super().__init__()
        self.title("Card Scanner (macOS)")
        self.geometry("520x520")
        self.configure(bg=self.BG)
        self.minsize(420, 420)

        self._on_running_changed = on_running_changed
        self._on_calibration_changed = on_calibration_changed
        self._on_telegram_changed = on_telegram_changed
        self._on_pause_input_changed = on_pause_input_changed
        self._on_recompute_region = on_recompute_region
        self._on_save_region_override = on_save_region_override

        self.var_running = tk.BooleanVar(value=False)
        self.var_calibration = tk.BooleanVar(value=False)
        self.var_telegram = tk.BooleanVar(value=telegram_available)
        self.var_pause_input = tk.BooleanVar(value=False)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._region_label_var = tk.StringVar(value="Region: (looking…)")
        self._status_var = tk.StringVar(value="Stopped")

        self._overlay = OutlineOverlay(self)
        self._build_ui(telegram_available=telegram_available)
        self.after(100, self._poll_log)

    # ── public API for the main loop ──

    def log(self, message: str) -> None:
        self._log_queue.put(message)

    def update_region_label(self, region: capture.Region | None, source: str) -> None:
        if region is None:
            self._region_label_var.set("Region: not found (PS Remote Play not visible?)")
            self._overlay.hide()
            return
        self._region_label_var.set(
            f"Region [{source}]: {region.width}x{region.height} @ ({region.left},{region.top})"
        )
        if self.var_running.get() or self._overlay.visible:
            self._overlay.show(region)

    def show_overlay(self, region: capture.Region) -> None:
        self._overlay.show(region)

    def hide_overlay(self) -> None:
        self._overlay.hide()

    # ── UI construction ──

    def _build_ui(self, *, telegram_available: bool) -> None:
        pad = {"padx": 10, "pady": 6}

        header = tk.Frame(self, bg=self.BG)
        header.pack(fill="x", **pad)
        tk.Label(
            header, text="Card Scanner (macOS)", bg=self.BG, fg=self.ACCENT,
            font=("Menlo", 14, "bold"),
        ).pack(side="left")

        # Status row
        status = tk.Frame(self, bg=self.BG)
        status.pack(fill="x", **pad)
        self._lbl_status = tk.Label(
            status, textvariable=self._status_var, bg=self.BG, fg=self.ALERT,
            font=("Menlo", 11, "bold"),
        )
        self._lbl_status.pack(side="left")

        # Region row
        region_row = tk.Frame(self, bg=self.BG)
        region_row.pack(fill="x", **pad)
        tk.Label(
            region_row, textvariable=self._region_label_var,
            bg=self.BG, fg=self.FG, font=("Menlo", 10), anchor="w", justify="left",
        ).pack(side="left", fill="x", expand=True)
        tk.Button(
            region_row, text="Recompute", command=self._on_recompute_region,
        ).pack(side="right")
        tk.Button(
            region_row, text="Save current as override",
            command=self._on_save_region_override,
        ).pack(side="right", padx=4)

        # Buttons row
        buttons = tk.Frame(self, bg=self.BG)
        buttons.pack(fill="x", **pad)
        self._btn_run = tk.Button(
            buttons, text="START", command=self._toggle_running,
            bg="#003322", fg=self.ACCENT, activebackground="#004433",
            font=("Menlo", 11, "bold"), width=12,
        )
        self._btn_run.pack(side="left")

        # Toggles
        toggles = tk.Frame(self, bg=self.BG)
        toggles.pack(fill="x", **pad)
        tk.Checkbutton(
            toggles, text="Calibration (log every OCR string)",
            variable=self.var_calibration, command=self._calibration_changed,
            bg=self.BG, fg=self.FG, selectcolor=self.BG,
            activebackground=self.BG, activeforeground=self.FG,
        ).pack(anchor="w")
        tk.Checkbutton(
            toggles, text="Telegram alerts" + ("" if telegram_available else "  (set TELEGRAM_TOKEN + TELEGRAM_CHAT_ID to enable)"),
            variable=self.var_telegram, command=self._telegram_changed,
            state=("normal" if telegram_available else "disabled"),
            bg=self.BG, fg=self.FG, selectcolor=self.BG,
            activebackground=self.BG, activeforeground=self.FG,
        ).pack(anchor="w")
        tk.Checkbutton(
            toggles, text="Pause OCR while I'm typing/moving the mouse",
            variable=self.var_pause_input, command=self._pause_input_changed,
            bg=self.BG, fg=self.FG, selectcolor=self.BG,
            activebackground=self.BG, activeforeground=self.FG,
        ).pack(anchor="w")

        # Log
        log_frame = tk.Frame(self, bg=self.BG)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._log = tk.Text(
            log_frame, bg="#0A0A0A", fg=self.FG, font=("Menlo", 10),
            relief="flat", state="disabled", wrap="word", height=10,
        )
        scroll = tk.Scrollbar(log_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── handlers ──

    def _toggle_running(self) -> None:
        new = not self.var_running.get()
        self.var_running.set(new)
        if new:
            self._btn_run.configure(text="STOP", bg="#330000", fg=self.ALERT)
            self._status_var.set("Running")
            self._lbl_status.configure(fg=self.ACCENT)
        else:
            self._btn_run.configure(text="START", bg="#003322", fg=self.ACCENT)
            self._status_var.set("Stopped")
            self._lbl_status.configure(fg=self.ALERT)
        self._on_running_changed(new)

    def _calibration_changed(self) -> None:
        self._on_calibration_changed(self.var_calibration.get())

    def _telegram_changed(self) -> None:
        self._on_telegram_changed(self.var_telegram.get())

    def _pause_input_changed(self) -> None:
        self._on_pause_input_changed(self.var_pause_input.get())

    def _on_close(self) -> None:
        try:
            self._overlay.hide()
        finally:
            self.destroy()

    # ── log pump ──

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(120, self._poll_log)

    def _append_log(self, msg: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", msg + "\n")
        self._log.see("end")
        # Cap to ~500 lines to keep memory bounded.
        line_count = int(self._log.index("end-1c").split(".")[0])
        if line_count > 500:
            self._log.delete("1.0", f"{line_count - 500}.0")
        self._log.configure(state="disabled")
