# ◈ Card Scanner Overlay

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![EasyOCR](https://img.shields.io/badge/OCR-EasyOCR-green.svg)
![Tkinter](https://img.shields.io/badge/UI-Tkinter-orange.svg)

An intelligent automation assistant featuring a semi-transparent, Always-on-Top interface. Designed to monitor specific screen regions, identify text via OCR, and execute automated keyboard actions. Perfect for card games or systems requiring fast reactions to visual elements.

## ✨ Key Features

- **Chroma Key Overlay**: A 100% "hollow" capture window (transparent background) that allows the OCR engine to read the original screen perfectly without UI interference.
- **Multi-Pass OCR**: An optimized system that first identifies the "Rank" to narrow down the "Name" search to specific buckets, saving CPU/GPU resources.
- **Telegram Integration**: Automatic notifications with interactive Inline Keyboard buttons to confirm collections or report OCR misreads remotely.
- **Anti-Spam & Cooldown**: Smart logic to prevent duplicate clicks and notification spam for the same target.
- **Human Input Detection**: Automatically pauses automation when mouse movement or keystrokes are detected to avoid interfering with manual control.
- **JSON Persistence**: Automatically saves collected items to a local database to ignore them in future sessions.

## 🛠️ Technology Stack

- **EasyOCR**: Optical Character Recognition with CUDA (GPU) acceleration support.
- **Tkinter**: Lightweight, customized GUI for the overlay.
- **MSS**: High-performance screen capture library.
- **PyAutoGUI & Pynput**: Peripheral simulation and global input monitoring.
- **Requests**: Communication with the Telegram Bot API.

## 🚀 Getting Started

1. **Requirements**: Python 3.11 or higher.
2. **Installation**:
   ```bash
   pip install -r requirements.txt

## ⚠️ Language Dependency (OCR Localization)

This tool is currently configured to work with the **Portuguese (PT-BR)** version of the game. 

Since the automation relies on Optical Character Recognition (OCR) to match names and ranks:
- **String Matching:** The `TARGETS_POR_RANK` dictionary uses Portuguese terms as keys and values.
- **Game Language:** If your game is set to English or any other language, the OCR will not find a match.

### How to Port to Other Languages:
If you wish to use this in a different language:
1. Create a new branch.
2. Update the keys in `TARGETS_POR_RANK` to match the exact text displayed in your game's UI.
3. Update the `_normalizar` function if your language uses special characters not covered by the current logic.

---

## macOS / Apple Silicon (PS Remote Play)

The original `card_scanner.py` is **Windows-only** because it relies on
Tkinter's `-transparentcolor` chroma-key trick and on `pyautogui`
synthesizing keystrokes into the focused window. Neither survives a port
to macOS, and neither helps when the actual game is running on a PS5
streamed to your Mac via PS Remote Play.

The `mac_scanner/` package is the macOS port. It is **English-language**
and **notify-only**: it watches the PS Remote Play window with Apple's
Vision OCR and posts a macOS notification (plus optional Telegram) when
a tracked card appears. You then press X on your real DualSense to pick
up the card. The scanner never tries to synthesize input — Remote Play
ignores synthetic keystrokes from background tools, so notify-only is
the only reliable approach.

### Requirements

- Apple Silicon Mac running macOS 13+
- Python 3.11+ (Homebrew: `brew install python@3.14`, or another 3.11+ build)
- PS Remote Play installed and connected to your PS5

**Homebrew Python and Tkinter:** the `python@3.x` formula does not ship `_tkinter`.
If `python -m mac_scanner` crashes with `ModuleNotFoundError: No module named '_tkinter'`,
install the matching add-on (same minor version as your Python), then retry:

```bash
brew install python-tk@3.14   # use 3.12, 3.13, … to match `python3 --version`
```

Quick check: `python3 -c "import tkinter; print('ok')"`.

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_mac.txt
```

### macOS permissions (one-time)

The scanner needs three Privacy & Security grants:

1. **Screen Recording** — required by `mss` to capture the PS Remote
   Play window. System Settings -> Privacy & Security -> Screen
   Recording -> add your terminal (Terminal.app or iTerm) **and** your
   Python binary if you're invoked via a wrapper.
2. **Input Monitoring** — required by `pynput` for the
   pause-on-human-input feature. System Settings -> Privacy & Security
   -> Input Monitoring -> add your terminal.
3. **Accessibility** — also required by `pynput` on recent macOS
   versions. Same panel, Accessibility tab.

After granting any of these you may need to fully quit and relaunch the
terminal.

### Running

```bash
python -m mac_scanner
```

Then in the control window:

1. Make sure PS Remote Play is open and visible. Click **Recompute** if
   the region label says "not found".
2. Click **START**. A green outline appears around the band of the
   Remote Play window the scanner is watching.
3. When a tracked card is recognized, you'll get a macOS notification
   and the alert sound. Press X on your DualSense.

### Configuring targets

Targets live in [`mac_scanner/config.py`](mac_scanner/config.py) as a
nested dict keyed by tier then rarity:

```python
TARGETS_INITIAL = {
    "RAINBOW": {
        "LEGENDARY": ALL_CARD_NAMES,
        # "COMMON": {"BB9", "R7"},  # add the names you want for other rarities
    },
    # "GOLD": {"LEGENDARY": {...}},
}
```

Only `RAINBOW` (tier) and `COMMON` / `LEGENDARY` (rarities) are
confirmed against real screenshots; the other tier and rarity words in
`TIERS` / `RARITIES` are educated guesses. Use **Calibration mode** (see
below) to discover the actual strings the game shows.

### Calibration mode (find the right strings)

The game's exact spellings for the other 3 tiers and remaining rarities
need to come from real game frames. Turn on the **Calibration** toggle
in the control window, hit START, and walk past one card of each
tier/rarity in-game. Every OCR string in every captured frame is
written to the log with its confidence score. Pick the strings that
look like tier/rarity labels and add them to `TIERS` / `RARITIES` in
`config.py`.

This is also how to handle truncated card names — if the game shows
`MONO-WLKR` instead of `MONO-WALKER`, the matcher's RapidFuzz scorer
already handles it (threshold 75), but if you see a card consistently
missed, lower `FUZZY_SCORE_THRESHOLD` in `config.py` or add the
truncated form as an extra entry in `ALL_CARD_NAMES`.

### Spatial matching

When several cards are on screen with different rarities (e.g. CB
COMMON, NAV-EX RARE, GONK COMMON), the matcher uses Vision's bounding
boxes to pair each rarity label with the card name **directly above
it** in a similar x-range. Without this you get false positives where
the matcher sees `RARE` somewhere in the frame and reports the
highest-scoring fuzzy hit (often the wrong card).

It also handles two real-world OCR quirks:
- Vision sometimes joins tokens like `DIAMONDRARE` — they get split
  back into `DIAMOND` + `RARE` automatically.
- Tier and rarity sometimes arrive as two separate observations
  (`DEFAULT` and `COMMON` on different lines) — they're combined when
  they're on the same row and adjacent.

The card-name vocabulary is also passed to Vision as `customWords`,
which biases recognition toward known codes like `R5`, `BB9`,
`DRK-1 PROBE`. This requires `usesLanguageCorrection=True`, which adds
~50–100ms per frame but noticeably reduces OCR errors on game text.

### Region

By default the scanner captures the **entire** PS Remote Play window
each frame and lets the matcher filter (it only attempts a name match
when both a tier word and a rarity word are visible). On Apple Silicon
this is ~200–500 ms per frame and is the most reliable setting.

If you want to crop to the top band of the window (faster, slightly
lower CPU use, but skips pickup-card popups in the screen center), opt
in via env:

```bash
MAC_SCANNER_REGION=band python -m mac_scanner
```

For a fully manual region, click **Save current as override** in the UI
to write `region.json`, then edit it by hand (`left`, `top`, `width`,
`height` in physical pixels). The override always wins over auto.

### Telegram (optional)

Set the credentials in your environment before launching:

```bash
export TELEGRAM_TOKEN="123456:ABC-..."
export TELEGRAM_CHAT_ID="987654321"
python -m mac_scanner
```

Alternatively, put the same keys in a `.env` file at the repo root
(`TELEGRAM_TOKEN=...` and `TELEGRAM_CHAT_ID=...` on separate lines). The
scanner loads it automatically via `python-dotenv`. `.env` is listed in
`.gitignore` so it is not committed.

Without these, Telegram is disabled cleanly and you only get local
notifications. Inline buttons on Telegram messages still work (Confirm
removes the card from the active list and persists it to
`collected_targets.json`).

**Common mistake:** `TELEGRAM_CHAT_ID` must be **your** Telegram user id
(or a group id), **not** the bot's id. If you see `Forbidden: the bot can't
send messages to the bot`, open @userinfobot, copy your numeric id, put
it in `.env`, and send `/start` to your bot in a private chat first.

### OCR engine

The default is Apple Vision via PyObjC (Neural Engine, fast, accurate
on small UI text). To use EasyOCR on CPU instead:

```bash
pip install easyocr
MAC_SCANNER_OCR=easyocr python -m mac_scanner
```

### What's intentionally different from the Windows version

- No `pyautogui`, no `CGEventPost`, no key synthesis. Notify-only.
- The chroma-key transparent overlay is replaced by four thin
  click-through edge bars surrounding the capture region.
- OCR matches on `(tier, rarity, name)` triples, not single ranks.
- Name matching is fuzzy (RapidFuzz `WRatio` >= 75), so truncated
  in-game names like `MONO-WLKR` for `MONO-WALKER` still match.
- Telegram credentials are read from environment variables; nothing is
  hardcoded.
- Persistence file is `collected_targets.json` (new triple schema), not
  the Windows version's `targets_removidos.json`.
