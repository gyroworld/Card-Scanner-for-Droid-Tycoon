# ◈ Card Scanner (Droid Tycoon)

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![macOS](https://img.shields.io/badge/macOS-13%2B%20(Apple%20Silicon)-black.svg)
![OCR](https://img.shields.io/badge/OCR-Apple%20Vision-green.svg)

macOS assistant that watches **PS Remote Play** (your PS5 stream), runs **Apple Vision** OCR on each frame, and alerts you when a configured **tier / rarity / card name** appears so you can grab it on the controller. **Detection is notify-only**: card hits never push input back into the game. There is an **optional anti-idle keypress** (default: spacebar = jump) that can be enabled separately to keep the UEFN island from kicking you for inactivity — see [Anti-idle keypress](#anti-idle-keypress) below.

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

### Anti-idle keypress

The Droid Tycoon UEFN island kicks idle players after a few minutes. To stay logged in while you're not actively at the controller, the scanner can periodically synthesize a key down/up event (default: **spacebar**, which is "jump" in-game and doesn't move the character meaningfully). The press is posted via CoreGraphics (`CGEventCreateKeyboardEvent` / `CGEventPost`), and PS Remote Play is activated to the foreground immediately before each send so the OS routes the keystroke into the game.

This is **opt-in and disabled by default** — you must pass `--anti-idle` or set `MAC_SCANNER_ANTI_IDLE=1`. Detection still never injects input. Default cadence is **once every 5 minutes** (300 s), just under the in-game idle-kick threshold.

```bash
# Send one spacebar press right now, then exit (smoke test).
python -m mac_scanner --send-space

# Run normally, with the anti-idle loop firing every 5 minutes.
python -m mac_scanner --anti-idle

# Custom interval (seconds) and headless:
python -m mac_scanner --headless --anti-idle --anti-idle-interval 240

# Different key (anything in input_sender.KEY_CODES — space, return,
# escape, tab, arrow keys, w/a/s/d, etc.):
MAC_SCANNER_ANTI_IDLE_KEY=w python -m mac_scanner --anti-idle
```

Tuning knobs (all overridable from environment, see `mac_scanner/config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAC_SCANNER_ANTI_IDLE` | off | Auto-enable the loop at startup (`--anti-idle` does the same). |
| `MAC_SCANNER_ANTI_IDLE_KEY` | `space` | Key to send. Must be in `input_sender.KEY_CODES`. |
| `MAC_SCANNER_ANTI_IDLE_INTERVAL` | `300` | Seconds between presses (5 min, just under the UEFN island idle-kick threshold). |
| `MAC_SCANNER_ANTI_IDLE_FOCUS` | `1` | Activate PS Remote Play via AppleScript before each post. Disable if you keep Remote Play permanently foregrounded. |
| `MAC_SCANNER_ANTI_IDLE_GRACE` | `10` | Skip a press if the real keyboard/mouse has been touched within this many seconds. Gamepad input on the DualSense is **not** observable to pynput, so the press still fires while you're playing on the controller. |
| `MAC_SCANNER_ANTI_IDLE_HOLD` | `0.05` | Down→up duration. Remote Play occasionally drops 0-ms taps; 50 ms is reliable. |

**Permissions:** anti-idle needs the same **Accessibility** grant the OCR side already uses. Focus is brought to Remote Play via `NSRunningApplication.activateWithOptions_`, which works under the existing Accessibility grant; an AppleScript `osascript` fallback exists for older macOS versions but requires the separate **Automation** permission (System Settings → Privacy & Security → Automation → enable PS Remote Play under your terminal/IDE).

**Focus-stealing on macOS Sonoma+:** an app in the background **cannot** pull focus to another app, by OS policy. If you run `--diagnose` while your Terminal is the frontmost window, you'll see `activateWithOptions_ returned True but frontmost is 'Terminal'` — that's macOS refusing the focus theft, not a bug. The fix is to click PS Remote Play once so it's the frontmost window, *then* start the scanner. Once Remote Play has focus, the anti-idle loop keeps it there and every press lands. The OCR side of the scanner already requires Remote Play to be visible, so this is the natural setup anyway.

**If `--send-space` says it succeeded but the character didn't jump, run the diagnostic first:**

```bash
python -m mac_scanner --diagnose
```

It walks every prerequisite and prints exact remediation if any fails (Accessibility not granted, Remote Play window not found, activation blocked, etc.). When all checks pass, the line you want to see is:

```
sent keypress 'space' (... activated=True, frontmost=PS Remote Play, trusted=True)
```

If you see that and the character still doesn't move, the issue is no longer in this codebase — PS Remote Play received the key but its mapping or the PS5 game isn't translating it into a jump. Likely causes:

* **PS Remote Play keyboard input is disabled.** Open Remote Play → Settings → Controllers / Keyboard and make sure keyboard input is enabled.
* **The PS Remote Play key map doesn't bind space → X (Cross).** Default on recent versions does, but check the configurator. Whatever key is mapped to X will work — point `MAC_SCANNER_ANTI_IDLE_KEY` at it.
* **Fortnite is on a menu screen, in a cinematic, or in a state where X does nothing.** Make sure you're in normal gameplay before testing.

Easy way to see whether keystrokes are landing: fire a burst of presses and watch the character jump several times in a row:

```bash
python -m mac_scanner --send-space --send-count 5 --send-delay 0.5
```

End-to-end smoke test of the sender itself (CGEventPost → pynput round-trip, plus the synth-window suppression in `HumanInputMonitor`):

```bash
python scripts/test_input_sender.py
```

### Auto-grab burst

Spams the in-game **pickup/interact** key (default: `E`) for a short window when invoked, useful for actually collecting a card rather than just being notified about it. **Off by default**, same as anti-idle — has to be enabled via CLI flag or env var.

```bash
# One-shot: fire a 3-second burst of E presses right now, then exit.
# Use this to verify the keystroke is actually being received as
# "interact" in-game (the prompt should be triggered repeatedly).
python -m mac_scanner --auto-grab

# Auto-collect mode: run the scanner, and whenever a target card is
# detected, automatically fire the burst. Re-entry is suppressed,
# OCR pauses while the burst is in flight.
python -m mac_scanner --auto-grab-on-hit

# Custom timing and key (e.g. spam "F" for 5 s at 0.05 s intervals):
python -m mac_scanner --auto-grab \
    --auto-grab-key f --auto-grab-duration 5 --auto-grab-delay 0.05
```

Defaults: **`E` key**, **3 s duration**, **0.1 s delay**, **0.03 s hold** — about 20–25 presses per burst depending on system scheduling overhead. Each burst:

1. Activates PS Remote Play once (same NSRunningApplication path as anti-idle — Accessibility required).
2. Posts `key down → hold → key up → delay`, in a tight loop, until the wall-clock deadline.
3. Marks the synth-suppression window so `HumanInputMonitor` doesn't see the burst as real user input.

Tuning knobs (env vars; see `mac_scanner/config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAC_SCANNER_AUTO_GRAB_ON_HIT` | off | Auto-fire a burst when a target card is detected (`--auto-grab-on-hit` does the same). |
| `MAC_SCANNER_AUTO_GRAB_KEY` | `e` | Key to spam. Must be in `input_sender.KEY_CODES`. |
| `MAC_SCANNER_AUTO_GRAB_DURATION` | `3.0` | Total burst length in seconds. |
| `MAC_SCANNER_AUTO_GRAB_DELAY` | `0.1` | Gap between consecutive presses. |
| `MAC_SCANNER_AUTO_GRAB_HOLD` | `0.03` | Key-down → key-up time per press. |
| `MAC_SCANNER_AUTO_GRAB_FOCUS` | `1` | Activate Remote Play before the burst starts. |

When `--auto-grab-on-hit` is active, the OCR capture loop **skips frames while a burst is firing** so it doesn't re-detect the same card and queue a parallel burst — only one burst runs at a time. After the burst finishes, the next OCR cycle runs normally; cooldowns (`COOLDOWN_NOTIFY_SECONDS`) prevent immediate re-grabbing of the same card.

### Logging

Rotating log at the repo root: **`mac_scanner.log`** (see `mac_scanner/__main__.py`). Headless mode also prints the same messages to stdout.

---

## ⚠️ Legacy: Windows script and Portuguese OCR

The original Windows design used a **chroma-key Tk overlay**, **EasyOCR**, and **Portuguese (PT-BR)** UI strings in a `TARGETS_POR_RANK`-style map, with automation via **PyAutoGUI**. That script is **not** included in this checkout.

If you ever revive it elsewhere: OCR strings and dictionaries must match the **in-game language**. The **mac** port uses **English** tier/rarity labels and card **codes** (e.g. `R5`, `BB9`) that do not depend on locale.

---

## Configuring targets

Alert filters live in **`data/droid_dex.json`** under the **`watch`** block. Vocabularies (`TIERS`, `RARITIES`, card names) are loaded from the same file at startup.

```json
"watch": {
  "tiers": ["RAINBOW", "BESKAR", "GALACTIC", "STELLAR"],
  "rarities": ["LEGENDARY", "MYTHIC", "ICONIC"],
  "classes": null,
  "names": null,
  "exclude_names": []
}
```

- **`tiers`**: paint variants (Base OCR-spells as `DEFAULT`).
- **`rarities`**: intrinsic droid rarities from the Droidex.
- **`classes`**: `Worker` / `Astromech` / `Battle`, or `null` for all.
- **`names`**: optional allow-list of card codes; `null` means every name that matches the other filters.
- **`exclude_names`**: names to never alert on.

Refresh the catalog from the wiki after game updates:

```bash
python3 scripts/refresh_droid_dex.py
```

Empty buckets are skipped. **`collected_targets.json`** stores triples you have already picked (via Telegram buttons or equivalent flows) so they are not alerted again.

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
| `MAC_SCANNER_ANTI_IDLE` etc. | See [Anti-idle keypress](#anti-idle-keypress) for the full set. |
| `MAC_SCANNER_AUTO_GRAB_ON_HIT` etc. | See [Auto-grab burst](#auto-grab-burst) for the full set. |

**Telegram tip:** `TELEGRAM_CHAT_ID` must be **your** user id (or a group id), not the bot’s. If the API says the bot cannot message the bot, use @userinfobot and send `/start` to your bot in private chat first.

### Debug dumps

With **`MAC_SCANNER_DEBUG_DUMP=1`**, missed-label situations write a **PNG** and **TXT** transcript under **`debug_frames/`** (minimum interval in config) — useful when a card is visible but no notification fired.

### What differs from the old Windows overlay

- No **PyAutoGUI** / key synthesis — **notify-only** on Mac.
- No chroma-key window; **four thin edge bars** outline the region (click-through when possible).
- Matching is **(tier, rarity, name)** with **fuzzy** names (RapidFuzz **WRatio** ≥ threshold in config).
- Credentials and toggles come from **environment** / **`.env`**, not hardcoded secrets.
- Persistence is **`collected_targets.json`** (triple schema).

### Development and tests

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

The **`needs_vision`** suite exercise Apple Vision wrappers on macOS; run on this platform before releases. After memory-related fixes, perform a manual **Activity Monitor soak** (60–120 min) using the checklist in **[docs/memory_soak.md](docs/memory_soak.md)**.
