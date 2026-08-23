"""Vision OCR Cocoa autorelease pool wiring (guards POSIX worker threads)."""

from __future__ import annotations

import sys
from contextlib import contextmanager

import numpy as np
import pytest


@pytest.mark.macos_only
@pytest.mark.needs_vision
def test_recognize_wraps_impl_in_autorelease_context(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("objc")
    pytest.importorskip("Vision", reason="requires pyobjc-framework-Vision")
    if sys.platform != "darwin":
        pytest.skip("Darwin only")

    from mac_scanner.ocr import VisionOCR

    entered: list[bool] = []

    @contextmanager
    def track() -> object:
        entered.append(True)
        yield

    monkeypatch.setattr(
        VisionOCR,
        "_objc_autorelease_context",
        staticmethod(track),
    )

    impl_calls = 0
    engine = VisionOCR(custom_words=())

    def fake_impl(image_bgr: np.ndarray):
        nonlocal impl_calls
        impl_calls += 1
        return []

    monkeypatch.setattr(engine, "_recognize_impl", fake_impl)

    rgb = np.zeros((4, 6, 3), dtype=np.uint8)
    engine.recognize(rgb[:0])
    assert entered == []
    assert impl_calls == 0

    engine.recognize(rgb)
    assert entered == [True]
    assert impl_calls == 1
