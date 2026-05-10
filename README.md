# ◈ Card Scanner (Droid Tycoon)

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![macOS](https://img.shields.io/badge/macOS-13%2B%20(Apple%20Silicon)-black.svg)
![OCR](https://img.shields.io/badge/OCR-Apple%20Vision-green.svg)

macOS assistant that watches **PS Remote Play** (your PS5 stream), runs **Apple Vision** OCR on each frame, and alerts you when a configured **tier / rarity / card name** appears so you can grab it on the controller. **Notify-only**: it does not synthesize keystrokes (Remote Play ignores background automation reliably).

This repository ships the **`mac_scanner/`** package and **`requirements_mac.txt`**. An older Windows Tkinter + chroma-key overlay with in-game automation lived in `card_scanner.py`; that file is **not** in this tree anymore, so day-to-day use here is macOS-only.

## ✨ What `mac_scanner` does

- **Screen capture** of the Remote Play window via **mss**, with optional **`region.json`** override.
- **Tier + rarity + name** matching with **RapidFuzz** (fuzzy names, spatial pairing of labels to card text).
- **Human input detection** (optional): pauses scanning while you move the mouse or type.
- **Telegram** (optional): card alerts with inline buttons; confirmations persist to **`collected_targets.json`**.
- **Calibration mode**: logs every OCR string (and confidence) to tune `TIERS` / `RARITIES` in config.
- **Headless mode** for servers or SSH sessions: no Tk UI, logs to stdout and **`mac_scanner.log`**.

## 🛠️ Technology stack (macOS)

- **Apple Vision** (default OCR) via PyObjC · **OpenCV** (preprocess) · **NumPy** · **RapidFuzz** · **mss**
- **Tkinter** control window + four thin edge windows for the capture outline (optional if you use `--headless`)
- **pynput** (input monitoring) · **requests** + **python-dotenv** (Telegram / `.env`)

Optional **EasyOCR** fallback: `pip install easyocr` and `MAC_SCANNER_OCR=easyocr`.

## 🚀 Quick start (macOS)

1. **Requirements**: Apple Silicon Mac, **macOS 13+**, **Python 3.11+**.
2. **Tkinter**: Homebrew `python@3.x` does not include `_tkinter`. Install the matching add-on, e.g. `brew install python-tk@3.14` (minor version must match `python3 --version`). Check: `python3 -c "import tkinter; print('ok')"`.
3. **Install**:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements_mac.txt
   ```

4. **Permissions** (one-time): grant **Screen Recording**, **Input Monitoring**, and **Accessibility** to your terminal (and Python if you launch via a wrapper). Quit and reopen the terminal after changes.

5. **Run**:

   ```bash
   python -m mac_scanner
   ```

   - **Headless** (starts scanning immediately; Ctrl+C to quit):

     ```bash
     python -m mac_scanner --headless
     ```

   - **Calibration** from the CLI (useful with headless):

     ```bash
     python -m mac_scanner --headless --calibration
     ```

6. In the GUI: open PS Remote Play, click **Recompute** if the region is missing, then **START**. A green outline shows the captured area.

### Alerts: sound vs Notification Center

On a **card hit**, the app always plays the system **Glass** alert sound (`afplay`). **Notification Center toasts are off by default**; enable them with:

```bash
export MAC_SCANNER_TOAST=1
python -m mac_scanner
```

**Spawn** lines (“X spawned at …”) are still **detected and logged** as `[spawn]` for debugging, but **user-visible spawn alerts are disabled** in `mac_scanner/config.py` (`ENABLE_SPAWN_NOTIFICATIONS = False`) so only rack/card detections notify.

### Logging

Rotating log at the repo root: **`mac_scanner.log`** (see `mac_scanner/__main__.py`). Headless mode also prints the same messages to stdout.

---

## ⚠️ Legacy: Windows script and Portuguese OCR

The original Windows design used a **chroma-key Tk overlay**, **EasyOCR**, and **Portuguese (PT-BR)** UI strings in a `TARGETS_POR_RANK`-style map, with automation via **PyAutoGUI**. That script is **not** included in this checkout.

If you ever revive it elsewhere: OCR strings and dictionaries must match the **in-game language**. The **mac** port uses **English** tier/rarity labels and card **codes** (e.g. `R5`, `BB9`) that do not depend on locale.

---

## Configuring targets

Edit **`mac_scanner/config.py`**: nested dict **`TARGETS_INITIAL`**, keys are **tier** then **rarity**, values are sets of card name strings.

```python
TARGETS_INITIAL = {
    "RAINBOW": {
        "LEGENDARY": {"PROTO-ROLLER", "BB9", "R7"},
        # "COMMON": {"GONK", "CB"},
    },
    # "GOLD": {"COMMON": {...}},
}
```

The file also defines **`TIERS`**, **`RARITIES`**, and **`ALL_CARD_NAMES`**. Empty buckets are skipped. **`collected_targets.json`** stores triples you have already picked (via Telegram buttons or equivalent flows) so they are not alerted again.

Use **Calibration** in the UI (or `--calibration`) to capture real OCR spellings before expanding tiers/rarities.

### Spatial matching

The matcher uses Vision bounding boxes to pair each **rarity** (and tier) with the **card name above it** in a similar horizontal band, which cuts false positives when several cards are on screen. Known OCR quirks (e.g. merged `DIAMONDRARE`) are normalized in **`mac_scanner/matcher.py`**. **`customWords`** bias Vision toward known card codes (adds a bit of latency per frame).

### Capture region

- Default: **entire** Remote Play window (`MAC_SCANNER_REGION` unset or `full`). Expect on the order of **~200–500 ms** OCR per frame on Apple Silicon at typical resolutions.
- Faster crop: top **band** only:

  ```bash
  MAC_SCANNER_REGION=band python -m mac_scanner
  ```

- **Manual** region: click **Save current as override** in the UI to write **`region.json`**, or edit **`left` / `top` / `width` / `height`** by hand. Override wins over auto-detection.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | Optional Telegram bot; also loadable from **`.env`** at repo root |
| `MAC_SCANNER_OCR` | `vision` (default) or `easyocr` |
| `MAC_SCANNER_REGION` | `full` (default) or `band` |
| `MAC_SCANNER_TOAST` | `1` / `true` / `yes` / `on` — enable Notification Center on hits |
| `MAC_SCANNER_OCR_SCALE` | Upscale before OCR (default **1.7**; try **2.0** if small labels are missed) |
| `MAC_SCANNER_OCR_TRIM` | `1` (default) trims near-black borders before OCR; set `0` to disable |
| `MAC_SCANNER_DEBUG_DUMP` | `1` — when a card-like string appears but tier/rarity labels are missing, save frame + OCR dump under **`debug_frames/`** (throttled) |

**Telegram tip:** `TELEGRAM_CHAT_ID` must be **your** user id (or a group id), not the bot’s. If the API says the bot cannot message the bot, use @userinfobot and send `/start` to your bot in private chat first.

### Debug dumps

With **`MAC_SCANNER_DEBUG_DUMP=1`**, missed-label situations write a **PNG** and **TXT** transcript under **`debug_frames/`** (minimum interval in config) — useful when a card is visible but no notification fired.

### What differs from the old Windows overlay

- No **PyAutoGUI** / key synthesis — **notify-only** on Mac.
- No chroma-key window; **four thin edge bars** outline the region (click-through when possible).
- Matching is **(tier, rarity, name)** with **fuzzy** names (RapidFuzz **WRatio** ≥ threshold in config).
- Credentials and toggles come from **environment** / **`.env`**, not hardcoded secrets.
- Persistence is **`collected_targets.json`** (triple schema).
