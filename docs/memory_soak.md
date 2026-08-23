# Memory soak verification (post–memory-hardening releases)

After changes that affect OCR, threading, or Telegram, run the scanner for **60–120 minutes** on a machine whose RAM you can observe (Activity Monitor is enough). This is a **manual** supplement to `pytest`; it targets behavior that CI cannot easily reproduce (Retina capture, Vision on a worker thread, long-run RSS).

## Pass criteria

| Step | What to do | Pass |
|------|------------|------|
| 1 | Activity Monitor → find the Python process running `mac_scanner`. Note RSS after warm-up (~5–10 min), then again at 60 and 120 min. | RSS should **not** climb monotonically into multi‑GB on a stationary screen without other apps driving load. Stable “high watermark” plateau is acceptable. |
| 2 | Repeat with the **GUI**, click **START**. | Behavior should not be noticeably worse than headless for the same region/OCR scale. |
| 3 | With **Telegram** enabled (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`), trigger a handful of alerts. | Outbound completes; Activity Monitor thread count should **not** spike with one thread per message (single outbound worker plus poll thread only). |
| 4 | With **`--auto-grab-on-hit`** (or equivalent env), provoke several hits if you can safely. | Bursts finish; RSS returns roughly toward baseline after grabs complete. |
| 5 | (Optional) Xcode **Instruments** → **Allocations** or **Leaks** while `_capture_loop` + Vision run. | No unbounded allocations tied to `_recognize`/Vision per sweep; leaks tool quiet after pool fixes. |

## If RSS still ramps

Tune capture cost before refactoring architecture:

- `MAC_SCANNER_REGION=band`
- Lower `MAC_SCANNER_OCR_SCALE`
- Raise `CAPTURE_INTERVAL_SECONDS`

Document your settings in the soak notes so regressions can be compared.
