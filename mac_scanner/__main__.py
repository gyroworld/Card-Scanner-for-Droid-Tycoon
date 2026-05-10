"""Entry point: `python -m mac_scanner`.

Wires the capture loop, OCR engine, matcher, notifier, input monitor,
and (optionally) the UI together. Worker threads do the heavy lifting;
the main thread runs either the Tk event loop or, in headless mode, a
plain sleep loop until Ctrl+C.
"""

from __future__ import annotations

import argparse
import logging
import signal
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
    def __init__(self, *, headless: bool = False, calibration: bool = False) -> None:
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
                fired = True
            if fired:
                self._last_hit_at = time.perf_counter()

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
        self._input_monitor.start()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
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
            self._telegram.stop()
            self._input_monitor.stop()
            if self._grabber is not None:
                self._grabber.close()

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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "darwin":
        print("mac_scanner is macOS-only. Use card_scanner.py on Windows.", file=sys.stderr)
        return 2
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    ScannerApp(headless=args.headless, calibration=args.calibration).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
