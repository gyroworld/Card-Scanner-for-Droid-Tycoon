"""OCR engines.

Primary: Apple Vision via PyObjC. Runs on the Neural Engine on Apple
Silicon and handles small UI text under PS Remote Play's video
compression much better than CPU EasyOCR.

Fallback: EasyOCR on CPU, behind MAC_SCANNER_OCR=easyocr.

Both engines expose the same minimal contract:
    engine.recognize(image_bgr) -> list[OCRResult]
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from . import config


@dataclass(frozen=True)
class OCRResult:
    text: str
    confidence: float  # 0..1
    # (left, top, width, height) in pixels of the input image, origin
    # at the top-left. Populated when the engine supports it; None
    # otherwise.
    bbox: tuple[float, float, float, float] | None = None

    @property
    def cx(self) -> float:
        if self.bbox is None:
            return 0.0
        return self.bbox[0] + self.bbox[2] / 2

    @property
    def cy(self) -> float:
        if self.bbox is None:
            return 0.0
        return self.bbox[1] + self.bbox[3] / 2


class OCREngine(Protocol):
    name: str
    def recognize(self, image_bgr: np.ndarray) -> list[OCRResult]: ...


class VisionOCR:
    name = "vision"

    def __init__(
        self,
        languages: tuple[str, ...] = ("en-US",),
        custom_words: tuple[str, ...] | None = None,
    ) -> None:
        # Imported lazily so the module is importable on non-macOS hosts
        # (handy for tooling / linting; runtime still requires macOS).
        import Vision  # type: ignore[import]
        import Quartz  # type: ignore[import]
        from Foundation import NSData  # type: ignore[import]

        self._Vision = Vision
        self._Quartz = Quartz
        self._NSData = NSData
        self._languages = list(languages)
        # Bias recognizer toward known card codes / tokens. Vision applies
        # these to its language model the same way iOS Photos uses contact
        # names for handwriting suggestions.
        self._custom_words = list(custom_words) if custom_words else []

    def _np_to_cgimage(self, image_bgr: np.ndarray):
        h, w = image_bgr.shape[:2]
        rgba = np.empty((h, w, 4), dtype=np.uint8)
        rgba[..., 0] = image_bgr[..., 2]  # R
        rgba[..., 1] = image_bgr[..., 1]  # G
        rgba[..., 2] = image_bgr[..., 0]  # B
        rgba[..., 3] = 255

        Quartz = self._Quartz
        data = self._NSData.dataWithBytes_length_(rgba.tobytes(), rgba.size)
        provider = Quartz.CGDataProviderCreateWithCFData(data)
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        bitmap_info = (
            Quartz.kCGImageAlphaPremultipliedLast
            | Quartz.kCGBitmapByteOrder32Big
        )
        return Quartz.CGImageCreate(
            w, h, 8, 32, w * 4, cs, bitmap_info,
            provider, None, False, Quartz.kCGRenderingIntentDefault,
        )

    @staticmethod
    def _objc_autorelease_context():
        """Pool autoreleased ObjC churn on POSIX threads that lack NSRunLoop."""
        try:
            import objc  # noqa: PLC0415
            maker = getattr(objc, "autorelease_pool", None)
            if maker is None:
                return nullcontext()
            pool = maker()
            if hasattr(pool, "__enter__"):
                return pool
        except ImportError:
            pass
        return nullcontext()

    def recognize(self, image_bgr: np.ndarray) -> list[OCRResult]:
        if image_bgr.size == 0:
            return []
        with self._objc_autorelease_context():
            return self._recognize_impl(image_bgr)

    def _recognize_impl(self, image_bgr: np.ndarray) -> list[OCRResult]:
        Vision = self._Vision
        h, w = image_bgr.shape[:2]
        cgimage = self._np_to_cgimage(image_bgr)

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(self._languages)
        # Apple's docs are explicit: customWords are only consulted when
        # usesLanguageCorrection is True. With our card-name list as the
        # vocabulary, correction biases recognition *toward* known codes
        # instead of away from them, so we want it on whenever we have
        # custom words. Without custom words, correction tends to "fix"
        # codes like "BB9" -> "BBQ", so we leave it off.
        if self._custom_words:
            request.setUsesLanguageCorrection_(True)
            try:
                request.setCustomWords_(self._custom_words)
            except Exception:
                pass
        else:
            request.setUsesLanguageCorrection_(False)

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, {})
        ok, _err = handler.performRequests_error_([request], None)
        if not ok:
            return []

        out: list[OCRResult] = []
        for obs in (request.results() or []):
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            cand = cands[0]
            txt = cand.string()
            conf = float(cand.confidence())
            if not txt:
                continue
            bbox = self._extract_bbox(obs, w, h)
            out.append(OCRResult(text=txt, confidence=conf, bbox=bbox))
        return out

    @staticmethod
    def _extract_bbox(obs, img_w: int, img_h: int) -> tuple[float, float, float, float] | None:
        """Convert a Vision bounding box (normalized, origin bottom-left)
        into pixel coords with origin top-left."""
        try:
            rect = obs.boundingBox()
            origin = rect.origin
            size = rect.size
            x_n, y_bot_n = float(origin.x), float(origin.y)
            w_n, h_n = float(size.width), float(size.height)
            left = x_n * img_w
            top = (1.0 - y_bot_n - h_n) * img_h
            return (left, top, w_n * img_w, h_n * img_h)
        except Exception:
            return None


class EasyOCREngine:
    name = "easyocr"

    def __init__(self, languages: tuple[str, ...] = ("en",)) -> None:
        import easyocr  # type: ignore[import]
        # EasyOCR's gpu= flag checks for CUDA; on Apple Silicon it falls back
        # to CPU silently. We pass gpu=False to make that explicit.
        self._reader = easyocr.Reader(list(languages), gpu=False, verbose=False)

    def recognize(self, image_bgr: np.ndarray) -> list[OCRResult]:
        if image_bgr.size == 0:
            return []
        raw = self._reader.readtext(image_bgr, detail=1, paragraph=False)
        out: list[OCRResult] = []
        for (box, text, conf) in raw:
            if not text:
                continue
            try:
                xs = [float(p[0]) for p in box]
                ys = [float(p[1]) for p in box]
                left, top = min(xs), min(ys)
                right, bottom = max(xs), max(ys)
                bbox = (left, top, right - left, bottom - top)
            except Exception:
                bbox = None
            out.append(OCRResult(text=text, confidence=float(conf), bbox=bbox))
        return out


def get_engine(
    kind: str | None = None,
    custom_words: tuple[str, ...] | None = None,
) -> OCREngine:
    chosen = (kind or config.OCR_ENGINE).lower()
    if chosen in ("vision", "apple", "macos"):
        return VisionOCR(custom_words=custom_words)
    if chosen in ("easyocr", "easy"):
        return EasyOCREngine()
    raise ValueError(f"Unknown OCR engine: {chosen!r}")
