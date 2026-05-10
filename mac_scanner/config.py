"""Static configuration for mac_scanner.

Vocabularies and the initial TARGETS bucket. The TARGETS structure is
nested: TARGETS[tier][rarity] -> set of card-name strings.

Only RAINBOW (tier) and COMMON / LEGENDARY (rarities) are confirmed from
real screenshots. The other tier and rarity words are educated guesses;
use the Calibration toggle in the UI to discover the real strings, then
update this file.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Load `.env` from the repo root so TELEGRAM_* and MAC_SCANNER_* work without
# manually exporting variables in the shell.
try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

# ─── Vocabularies ────────────────────────────────────────────────────────────

TIERS: frozenset[str] = frozenset({
    "DEFAULT",   # confirmed from in-game ("R3 DEFAULT COMMON" etc.)
    "GOLD",      # confirmed from in-game ("GONK GOLD COMMON")
    "DIAMOND",   # assumed
    "RAINBOW",   # confirmed from in-game ("PROTO-ROLLER RAINBOW LEGENDARY")
})

RARITIES: frozenset[str] = frozenset({
    "COMMON",
    "RARE",
    "EPIC",
    "LEGENDARY",
})

# Card names — proper-noun codes that don't translate. Verbatim from the
# original Portuguese card_scanner.py target list.
ALL_CARD_NAMES: frozenset[str] = frozenset({
    "MOUSE", "PIT", "GONK", "CB", "R3", "R5", "R8",
    "IMPERIAL PROBE", "B1 BATTLE", "DRK-1 PROBE", "ID10",
    "BDX EXPLORER", "ARG", "SENATE HOVERCAM", "BU-4D", "BAL-CORE",
    "ROLL-R", "2BB", "A-LT", "R4", "R9", "B1 SECURITY", "NAV-EX",
    "VECT-ARM", "HOV-R", "GROUNDMECH", "LO", "AMP WALKER", "SEN-TRI",
    "OPTI-POD", "BB", "R2", "R6", "TRAK-R", "ORB-WALKER", "UTIL-TEC",
    "B1 HEAVY", "B2 SUPER", "B2 HEAVY", "STRIKE-ORB", "HAUL-R",
    "LNG-SHOT", "PROTO-ROLLER", "MECHA-DROID", "MONO-WALKER", "BB9",
    "R7", "B2-RP", "CYCLO-GRAV", "OPTI-STRIKE",
})

# Active target buckets. Empty buckets are skipped at runtime.
#
# TEMP (testing): notify for ANY card whose rarity reads as RARE,
# under any tier. Restore the LEGENDARY-only bucket below when done.
TARGETS_INITIAL = {
    "DIAMOND": {
        "COMMON":    ALL_CARD_NAMES,
        "RARE":      ALL_CARD_NAMES,
        "EPIC":      ALL_CARD_NAMES,
        "LEGENDARY": ALL_CARD_NAMES,
    },
    "RAINBOW": {
        "COMMON":    ALL_CARD_NAMES,
        "RARE":      ALL_CARD_NAMES,
        "EPIC":      ALL_CARD_NAMES,
        "LEGENDARY": ALL_CARD_NAMES,
    },
}

# ─── Behaviour knobs ─────────────────────────────────────────────────────────

# Persistence file for "already collected" triples (tier, rarity, name).
COLLECTED_FILE: Path = _REPO_ROOT / "collected_targets.json"

# Optional override file for the capture region (mss-style bbox dict).
# If present, takes priority over the auto-detected Remote Play region.
REGION_OVERRIDE_FILE: Path = _REPO_ROOT / "region.json"

# Per (tier, rarity, name) cooldown — don't re-notify the same card too often.
COOLDOWN_NOTIFY_SECONDS: float = 30.0

# Spawn-toast ("XYZ DROID SPAWNED AT…") notifications are disabled by
# design: only the actual card-on-the-rack detections fire alerts.
# The matcher still parses spawn toasts and the [spawn] line is still
# logged (useful for calibration and debugging), but no toast / sound /
# Telegram message is sent for them.
ENABLE_SPAWN_NOTIFICATIONS: bool = False

# After ANY hit, sleep this long before scanning again.
COOLDOWN_AFTER_HIT_SECONDS: float = 2.5

# Approximate cadence between captures. Bumped up because the default
# region is now the entire Remote Play window (Vision OCR per frame is
# ~200–500 ms on Apple Silicon at retina resolution).
CAPTURE_INTERVAL_SECONDS: float = 0.5

# RapidFuzz score threshold for accepting a name match. WRatio is the scorer
# (handles truncation, deletion, OCR substitutions reasonably well).
FUZZY_SCORE_THRESHOLD: int = 75

# OCR confidence floor (Vision returns 0..1; EasyOCR returns 0..1).
OCR_MIN_CONFIDENCE: float = 0.30

# ─── Environment ─────────────────────────────────────────────────────────────

TELEGRAM_TOKEN: str | None = os.environ.get("TELEGRAM_TOKEN") or None
TELEGRAM_CHAT_ID: str | None = os.environ.get("TELEGRAM_CHAT_ID") or None

# OCR engine: "vision" (default, Apple Vision via PyObjC) or "easyocr".
OCR_ENGINE: str = os.environ.get("MAC_SCANNER_OCR", "vision").lower()

# Capture region mode: "full" (entire Remote Play window, default) or
# "band" (cropped top horizontal band — only useful on slower hardware
# or when HUD text is producing false positives).
REGION_MODE: str = os.environ.get("MAC_SCANNER_REGION", "full").lower()

# macOS Notification Center toasts (`osascript display notification`).
# Off by default; set MAC_SCANNER_TOAST=1 to re-enable.
ENABLE_MACOS_TOAST: bool = (
    os.environ.get("MAC_SCANNER_TOAST", "").lower()
    in ("1", "true", "yes", "on")
)

# Bicubic upscale factor applied to the captured frame before OCR.
# The card rarity labels are very small (~15-25 captured pixels tall)
# and Vision's accurate recognizer struggles at that size. 1.5x is a
# good speed/accuracy tradeoff; raise to 2.0 if labels are still missed.
OCR_IMAGE_SCALE: float = float(os.environ.get("MAC_SCANNER_OCR_SCALE", "1.7"))

# Strip near-black borders before OCR. PS Remote Play in windowed mode
# sometimes leaves a large dead area below the actual game canvas;
# trimming it both speeds up OCR and frees up "pixel budget" for the
# upscale above.
OCR_TRIM_BORDERS: bool = os.environ.get(
    "MAC_SCANNER_OCR_TRIM", "1"
).lower() in ("1", "true", "yes", "on")

# Debug: when enabled, save frames + their OCR output to disk whenever
# a card name appears to be visible but no tier/rarity label was read.
# Useful for diagnosing missed notifications. Defaults off.
DEBUG_DUMP: bool = (
    os.environ.get("MAC_SCANNER_DEBUG_DUMP", "").lower()
    in ("1", "true", "yes", "on")
)
DEBUG_DUMP_DIR: Path = _REPO_ROOT / "debug_frames"
DEBUG_DUMP_MIN_INTERVAL_SECONDS: float = 3.0

# Built-in macOS alert sound played on a hit.
ALERT_SOUND_PATH: str = "/System/Library/Sounds/Glass.aiff"

# ─── PS Remote Play identification ───────────────────────────────────────────

# Owner names we'll try (in order) when looking for the Remote Play window.
# kCGWindowOwnerName values we've seen across versions.
REMOTE_PLAY_OWNER_NAMES: tuple[str, ...] = (
    "PS Remote Play",
    "Remote Play",
    "RemotePlay",
)
