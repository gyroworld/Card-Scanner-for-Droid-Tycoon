"""Static configuration for mac_scanner.

Vocabularies and the initial TARGETS bucket. The TARGETS structure is
nested: TARGETS[tier][rarity] -> set of card-name strings.

Several tier/rarity combinations are confirmed from in-game text (see
inline notes on TIERS); others are still guesses. Use the Calibration
toggle in the UI to log OCR strings live, then tighten vocabularies and
TARGETS_INITIAL here when you spot mismatches.
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
# Below: only RAINBOW / LEGENDARY is wired up — edit or uncomment tiers
# and rarities to match what you actually want alerted on.
TARGETS_INITIAL = {
 #   "DEFAULT": {
 #       "COMMON":    ALL_CARD_NAMES,
 #       "RARE":      ALL_CARD_NAMES,
 #       "EPIC":      ALL_CARD_NAMES,
 #       "LEGENDARY": ALL_CARD_NAMES,
 #   },
 #   "GOLD": {
 #       "COMMON":    ALL_CARD_NAMES,
 #       "RARE":      ALL_CARD_NAMES,
 #       "EPIC":      ALL_CARD_NAMES,
 #       "LEGENDARY": ALL_CARD_NAMES,
 #   },
 #   "DIAMOND": {
 #       "COMMON":    ALL_CARD_NAMES,
 #       "RARE":      ALL_CARD_NAMES,
 #       "EPIC":      ALL_CARD_NAMES,
 #       "LEGENDARY": ALL_CARD_NAMES,
 #   },
    "RAINBOW": {
 #       "COMMON":    ALL_CARD_NAMES,
 #       "RARE":      ALL_CARD_NAMES,
 #       "EPIC":      ALL_CARD_NAMES,
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

# After ANY hit, sleep this long before scanning again. Defaults to 0 —
# sensible for notify-only runs (per-card spam is gated by
# COOLDOWN_NOTIFY_SECONDS). With `--auto-grab-on-hit` or
# MAC_SCANNER_AUTO_GRAB_ON_HIT, the capture loop also pauses OCR while a
# grab burst runs; raising this delays re-scanning afterward if alerts
# still feel too noisy.
COOLDOWN_AFTER_HIT_SECONDS: float = 0.0

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

# While the full scanner app is running, run `caffeinate -d` so the
# display does not sleep. Opt out with MAC_SCANNER_CAFFEINATE=0 or
# `python -m mac_scanner --no-caffeinate`.
CAFFEINATE_DISPLAY: bool = os.environ.get(
    "MAC_SCANNER_CAFFEINATE", "1"
).lower() in ("1", "true", "yes", "on")

# Bicubic upscale factor applied to the captured frame before OCR.
# The card rarity labels are very small (~15-25 captured pixels tall)
# and Vision's accurate recognizer struggles at that size. Default 1.7x
# (via MAC_SCANNER_OCR_SCALE) balances speed vs accuracy; try 2.0 if labels
# are still missed.
OCR_IMAGE_SCALE: float = float(os.environ.get("MAC_SCANNER_OCR_SCALE", "1.7"))

# Strip near-black borders before OCR. PS Remote Play in windowed mode
# sometimes leaves a large dead area below the actual game canvas;
# trimming it both speeds up OCR and frees up "pixel budget" for the
# upscale above.
OCR_TRIM_BORDERS: bool = os.environ.get(
    "MAC_SCANNER_OCR_TRIM", "1"
).lower() in ("1", "true", "yes", "on")

# Debug: when enabled, save frames + OCR dumps when OCR shows no
# tier/rarity labels but includes at least one string that fuzzy-matches
# a known card name strongly (internal WRatio floor ~85 — see __main__.py).
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

# ─── Anti-idle keypress ──────────────────────────────────────────────────────

# Periodically synthesize a key down/up so the in-game character moves
# enough to stop the "idle kick" timer. The key is sent via CoreGraphics
# (`CGEventCreateKeyboardEvent` / `CGEventPost`), which the OS routes
# to whichever app is frontmost — so PS Remote Play must be activated
# first (handled in input_sender.py when `ANTI_IDLE_FOCUS_REMOTE_PLAY`).
#
# Requirements:
#   * Accessibility permission for the terminal / Python launcher
#     (System Settings → Privacy & Security → Accessibility).
#   * Remote Play visible and able to come to the foreground.

# Toggle: enable the anti-idle loop at startup via MAC_SCANNER_ANTI_IDLE or
# `--anti-idle` (no GUI toggle yet).
ANTI_IDLE_ENABLED: bool = (
    os.environ.get("MAC_SCANNER_ANTI_IDLE", "").lower()
    in ("1", "true", "yes", "on")
)

# Which key to press. Looked up in input_sender.KEY_CODES (currently
# "space", "return", "enter", "escape", "tab", "up", "down", "left",
# "right"). Spacebar = "jump" in Droid Tycoon, which is enough motion
# to reset the idle-kick timer without affecting gameplay much.
ANTI_IDLE_KEY: str = os.environ.get("MAC_SCANNER_ANTI_IDLE_KEY", "space").lower()

# How often to send the keypress. Default is 5 minutes (300s). The
# Droid Tycoon idle-kick fires somewhere around the 5-minute mark, so
# this stays just under the threshold without spamming the game with
# constant jumps.
ANTI_IDLE_INTERVAL_SECONDS: float = float(
    os.environ.get("MAC_SCANNER_ANTI_IDLE_INTERVAL", "300")
)

# Bring PS Remote Play to the foreground before posting the key. Set
# this to 0 if you intend to run the scanner with Remote Play already
# permanently focused and don't want stolen focus events.
ANTI_IDLE_FOCUS_REMOTE_PLAY: bool = os.environ.get(
    "MAC_SCANNER_ANTI_IDLE_FOCUS", "1"
).lower() in ("1", "true", "yes", "on")

# Skip the synthetic keypress if the user has touched the real
# keyboard / mouse within this many seconds — the user is actively
# playing and clearly not idle. Note: gamepad input on the DualSense
# is NOT tracked here (pynput only sees HID keyboard / mouse), so the
# keypress will still fire while you're playing on the controller.
# That's by design: the whole point is to keep the session alive
# while you're "AFK on the couch".
ANTI_IDLE_HUMAN_GRACE_SECONDS: float = float(
    os.environ.get("MAC_SCANNER_ANTI_IDLE_GRACE", "10")
)

# Down→up duration of the synthesized key. ~50 ms is short enough that
# the game registers it as a tap but long enough that PS Remote Play's
# input pipeline doesn't drop it.
ANTI_IDLE_KEY_HOLD_SECONDS: float = float(
    os.environ.get("MAC_SCANNER_ANTI_IDLE_HOLD", "0.05")
)

# ─── Auto-grab keypress burst ────────────────────────────────────────────────
#
# On demand (`--auto-grab` one-shot) or on card detection (`--auto-grab-on-hit`),
# spam a key — default "E", which is the in-game pickup/interact action in
# Droid Tycoon — for AUTO_GRAB_DURATION_SECONDS at AUTO_GRAB_DELAY_SECONDS
# intervals. At best you get roughly duration/delay presses (e.g. 3.0/0.1 ≈ 30);
# counting per-tap hold time, defaults usually land in the low–mid twenties —
# enough to grab a card even when the interact prompt is briefly hidden by an animation.

# Toggle: enable the on-hit auto-grab in the running scanner loop.
# `--auto-grab-on-hit` does the same thing at CLI level.
AUTO_GRAB_ON_HIT_ENABLED: bool = (
    os.environ.get("MAC_SCANNER_AUTO_GRAB_ON_HIT", "").lower()
    in ("1", "true", "yes", "on")
)

# Key to spam. Looked up in input_sender.KEY_CODES.
AUTO_GRAB_KEY: str = os.environ.get("MAC_SCANNER_AUTO_GRAB_KEY", "e").lower()

# Total burst duration.
AUTO_GRAB_DURATION_SECONDS: float = float(
    os.environ.get("MAC_SCANNER_AUTO_GRAB_DURATION", "3.0")
)

# Gap between consecutive presses inside the burst.
AUTO_GRAB_DELAY_SECONDS: float = float(
    os.environ.get("MAC_SCANNER_AUTO_GRAB_DELAY", "0.1")
)

# Down→up hold time per press. Kept short (30 ms) so the per-iteration
# overhead doesn't eat into AUTO_GRAB_DELAY_SECONDS.
AUTO_GRAB_HOLD_SECONDS: float = float(
    os.environ.get("MAC_SCANNER_AUTO_GRAB_HOLD", "0.03")
)

# Bring PS Remote Play to the foreground before the burst starts. Same
# semantics as ANTI_IDLE_FOCUS_REMOTE_PLAY — does not re-activate
# between individual presses.
AUTO_GRAB_FOCUS_REMOTE_PLAY: bool = os.environ.get(
    "MAC_SCANNER_AUTO_GRAB_FOCUS", "1"
).lower() in ("1", "true", "yes", "on")
