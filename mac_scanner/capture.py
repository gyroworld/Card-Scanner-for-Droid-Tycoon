"""Screen capture — locates the PS Remote Play window via Quartz, falls
back to a region.json override, and grabs frames with mss.

mss returns physical (Retina-scaled) pixels; Quartz reports logical
points. We multiply the Quartz bounds by the active backing scale factor
when packaging an mss bbox.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

from . import config


# Lazy imports — Quartz / AppKit must only load once the user is on macOS.
def _import_quartz():
    import Quartz  # type: ignore[import]
    return Quartz


def _import_appkit():
    from AppKit import NSScreen  # type: ignore[import]
    return NSScreen


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int

    def as_mss_bbox(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, d: dict) -> "Region":
        return cls(int(d["left"]), int(d["top"]),
                   int(d["width"]), int(d["height"]))


def _backing_scale_factor() -> float:
    """The Retina factor of the main display (1.0 on non-Retina, 2.0 typical)."""
    try:
        NSScreen = _import_appkit()
        screen = NSScreen.mainScreen()
        if screen is None:
            return 1.0
        return float(screen.backingScaleFactor())
    except Exception:
        return 2.0  # safe default for Apple Silicon laptops


def _list_windows() -> list[dict]:
    Quartz = _import_quartz()
    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    return Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []


_LOGGER = logging.getLogger("mac_scanner.capture")


def find_remote_play_window(owner_names: Iterable[str] = config.REMOTE_PLAY_OWNER_NAMES) -> Region | None:
    """Return the on-screen bounds of the PS Remote Play window in physical pixels.

    Looks for a window whose owner name matches any of `owner_names` and
    whose bounds are non-trivial. Returns None if not found or if Quartz
    is unavailable.
    """
    try:
        windows = _list_windows()
    except Exception as e:
        _LOGGER.warning("CGWindowList failed: %s", e)
        return None

    scale = _backing_scale_factor()
    wanted = {name.lower() for name in owner_names}

    candidates: list[tuple[int, dict]] = []
    for w in windows:
        owner = (w.get("kCGWindowOwnerName") or "").lower()
        if owner not in wanted:
            continue
        bounds = w.get("kCGWindowBounds") or {}
        width = int(bounds.get("Width", 0))
        height = int(bounds.get("Height", 0))
        if width < 200 or height < 200:
            continue
        candidates.append((width * height, w))

    if not candidates:
        sample_owners = sorted({(w.get("kCGWindowOwnerName") or "?") for w in windows})[:25]
        _LOGGER.info(
            "no Remote Play window matched %s; visible owners include: %s",
            list(owner_names), sample_owners,
        )
        return None

    candidates.sort(key=lambda t: -t[0])
    for area, w in candidates:
        b = w["kCGWindowBounds"]
        _LOGGER.info(
            "RemotePlay candidate: owner=%r title=%r bounds=(x=%s y=%s w=%s h=%s) area=%s",
            w.get("kCGWindowOwnerName"), w.get("kCGWindowName"),
            b.get("X"), b.get("Y"), b.get("Width"), b.get("Height"), area,
        )

    _, best = candidates[0]
    b = best["kCGWindowBounds"]
    region = Region(
        left=int(b["X"] * scale),
        top=int(b["Y"] * scale),
        width=int(b["Width"] * scale),
        height=int(b["Height"] * scale),
    )
    _LOGGER.info(
        "RemotePlay chosen window: logical (x=%s y=%s w=%s h=%s) -> physical %s (scale=%s)",
        b.get("X"), b.get("Y"), b.get("Width"), b.get("Height"),
        region.as_mss_bbox(), scale,
    )
    return region


def default_card_row_region(window: Region) -> Region:
    """Top band of the window — where the card row sits in this game.

    The card stations and pickup-card popups appear in roughly the upper
    third of the Remote Play view; the bottom is dominated by the player
    HUD, XP bar, weapon wheel, etc. Use a generous top band so:
      * close-up "PICK UP" cards (which sit near vertical center) are
        still partially captured,
      * tier/rarity labels printed *below* each card image still land
        inside the band.

    Override via region.json (`Save current as override` in the UI, then
    edit the file) when the auto-band misses your layout.
    """
    top_pct = 0.05
    bottom_pct = 0.55
    band_top = window.top + int(window.height * top_pct)
    band_height = int(window.height * (bottom_pct - top_pct))
    return Region(
        left=window.left,
        top=band_top,
        width=window.width,
        height=band_height,
    )


def load_region_override(path=config.REGION_OVERRIDE_FILE) -> Region | None:
    """Read a manual region from disk if present. Pixel coords, mss bbox style."""
    if not path.exists():
        return None
    try:
        return Region.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def save_region_override(region: Region, path=config.REGION_OVERRIDE_FILE) -> None:
    path.write_text(json.dumps(region.as_mss_bbox(), indent=2), encoding="utf-8")


class ScreenGrabber:
    """Wraps mss.mss() with thread-safe single-instance access."""

    def __init__(self) -> None:
        import mss  # type: ignore[import]
        self._sct = mss.mss()

    def grab_bgr(self, region: Region) -> np.ndarray:
        """Capture the region and return an HxWx3 uint8 BGR array."""
        raw = self._sct.grab(region.as_mss_bbox())
        bgra = np.array(raw, dtype=np.uint8)
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


def trim_dark_borders(
    img_bgr: np.ndarray,
    *,
    dark_threshold: int = 15,
    bright_threshold: int = 240,
    min_trim_fraction: float = 0.05,
) -> np.ndarray:
    """Crop uniform-extreme borders (near-black or near-white) so OCR
    only runs on actual game pixels.

    Two real situations this handles, both observed live:
      * PS Remote Play letterboxes the game canvas inside its window —
        the dead area is solid black.
      * Remote Play was resized to a smaller window after the scanner
        first computed the capture region, so the captured area now
        extends past the window into the macOS desktop (typically a
        light/white area). Trimming bright borders cleans this up
        without forcing the user to click "Recompute region".

    We compute the bbox of pixels that are NEITHER very dark NOR very
    bright — that's a robust proxy for "actual game content", since the
    game has continuously-varying mid-tone colors everywhere. Only crop
    if the dead area is non-trivial (>5% of either dimension).
    """
    if img_bgr.size == 0:
        return img_bgr
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    mask = ((gray > dark_threshold) & (gray < bright_threshold)).astype(np.uint8)
    coords = cv2.findNonZero(mask)
    if coords is None:
        return img_bgr
    x, y, w, h = cv2.boundingRect(coords)
    ih, iw = img_bgr.shape[:2]
    if (iw - w) < int(iw * min_trim_fraction) and (ih - h) < int(ih * min_trim_fraction):
        return img_bgr
    return img_bgr[y:y + h, x:x + w]


def upscale(img_bgr: np.ndarray, scale: float) -> np.ndarray:
    """Bicubic upscale used to push small UI text above Vision's
    effective resolution floor. The card rarity labels in this game are
    ~15–25 captured pixels tall, which is right at the edge of where
    Vision's accurate-mode recognizer starts dropping characters; a 1.5–2x
    upscale moves them into a much safer band."""
    if img_bgr.size == 0 or abs(scale - 1.0) < 0.01:
        return img_bgr
    h, w = img_bgr.shape[:2]
    return cv2.resize(
        img_bgr,
        (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_CUBIC,
    )


def preprocess_for_ocr(
    img_bgr: np.ndarray,
    *,
    scale: float = 1.5,
    trim_borders: bool = True,
) -> np.ndarray:
    if trim_borders:
        img_bgr = trim_dark_borders(img_bgr)
    if scale and abs(scale - 1.0) > 0.01:
        img_bgr = upscale(img_bgr, scale)
    return img_bgr


def resolve_region(prefer_override: bool = True) -> tuple[Region | None, str]:
    """Pick a capture region. Returns (region, source_label).

    Source label is one of: "override", "auto:full", "auto:band", "none".
    Mode is taken from config.REGION_MODE ("full" by default; "band" to
    use the cropped top band — opt-in via MAC_SCANNER_REGION=band).
    """
    if prefer_override:
        ovr = load_region_override()
        if ovr is not None:
            return ovr, "override"
    win = find_remote_play_window()
    if win is None:
        return None, "none"
    if config.REGION_MODE == "band":
        return default_card_row_region(win), "auto:band"
    return win, "auto:full"
