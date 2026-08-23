"""Notification surfaces.

Local: macOS Notification Center via `osascript` (no entitlements required)
       plus a built-in alert sound via `afplay`.

Remote: optional Telegram bot with inline "Confirm pickup" / "Not needed"
        buttons. Both remove the (tier, rarity, name) from the active
        TargetStore and persist the change; they only differ in the
        message/log wording so picked-up vs skipped cards are
        distinguishable after the fact.

Telegram is disabled cleanly when TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is
unset in the environment.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from . import config
from .notify_utils import prune_stale_cooldown_entries
from .targets import TargetStore, normalize

# Sentinel to stop Telegram outbound worker.
_OUTBOUND_STOP = object()

# Bounded queue avoids unbounded memory if Telegram is offline.
_TELEGRAM_OUTBOUND_MAX_PENDING = 32


# ─── Local (macOS) ───────────────────────────────────────────────────────────

def _osascript_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def macos_notify(title: str, subtitle: str, message: str) -> None:
    script = (
        f'display notification "{_osascript_quote(message)}" '
        f'with title "{_osascript_quote(title)}" '
        f'subtitle "{_osascript_quote(subtitle)}"'
    )
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def play_alert_sound(path: str | Path = config.ALERT_SOUND_PATH) -> None:
    try:
        subprocess.Popen(
            ["afplay", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


# ─── Telegram ────────────────────────────────────────────────────────────────

class TelegramBot:
    """Optional Telegram notifier with inline-button callbacks.

    `on_log` is invoked with status strings so the UI can show what's
    happening; `on_confirm` is invoked when the user taps the Confirm
    button on a card alert.
    """

    BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(
        self,
        store: TargetStore,
        token: str | None = config.TELEGRAM_TOKEN,
        chat_id: str | None = config.TELEGRAM_CHAT_ID,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._token = token
        self._chat_id = chat_id
        self._on_log = on_log or (lambda _msg: None)
        self._cooldowns: dict[tuple[str, str, str], float] = {}
        self._cooldown_lock = threading.Lock()
        self._last_update_id = 0
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._send_queue: queue.Queue[tuple[Any, ...]] | None = None
        self._outbound_thread: threading.Thread | None = None
        self._send_queue_overflow_logged = False
        if self.enabled:
            self._send_queue = queue.Queue(maxsize=_TELEGRAM_OUTBOUND_MAX_PENDING)
            self._outbound_thread = threading.Thread(
                target=self._outbound_worker,
                daemon=True,
                name="mac_scanner.telegram_outbound",
            )
            self._outbound_thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    # ── outbound ──

    def _can_notify(self, tier: str, rarity: str, name: str) -> bool:
        key = (tier, rarity, name)
        now = time.perf_counter()
        retention = float(config.COOLDOWN_MAP_RETENTION_SECONDS)
        with self._cooldown_lock:
            prune_stale_cooldown_entries(
                self._cooldowns,
                now,
                retention_seconds=retention,
            )
            last = self._cooldowns.get(key)
            if last is not None and (
                    now - last < config.COOLDOWN_NOTIFY_SECONDS):
                return False
            self._cooldowns[key] = now
            return True

    def _url(self, method: str) -> str:
        return self.BASE.format(token=self._token, method=method)

    def _enqueue_outbound(
        self,
        text: str,
        *,
        keyboard: dict | None,
    ) -> None:
        q = self._send_queue
        if q is None:
            return
        item = (text, keyboard)
        try:
            q.put_nowait(item)
            self._send_queue_overflow_logged = False
        except queue.Full:
            if not self._send_queue_overflow_logged:
                self._on_log(
                    "[telegram] outbound queue full; dropping alerts until backlog clears "
                    f"(max {_TELEGRAM_OUTBOUND_MAX_PENDING})",
                )
                self._send_queue_overflow_logged = True

    def _outbound_worker(self) -> None:
        q = self._send_queue
        if q is None:
            return
        while True:
            item = q.get()
            if item is _OUTBOUND_STOP:
                break
            text, keyboard = item
            self._post_message(text, None, keyboard=keyboard)

    def send_card_alert(self, tier: str, rarity: str, name: str) -> None:
        if not self.enabled:
            return
        if not self._can_notify(tier, rarity, name):
            self._on_log(f"[telegram] cooldown active, skipping {name}")
            return

        text = (
            f"*Card detected!*\n"
            f"Name: `{name}`\n"
            f"Tier: `{tier}`\n"
            f"Rarity: `{rarity}`\n\n"
            f"Remove from search list?"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "Confirm pickup",
                 "callback_data": f"confirm|{tier}|{rarity}|{name}"},
                {"text": "Not needed",
                 "callback_data": f"skip|{tier}|{rarity}|{name}"},
            ]]
        }
        self._enqueue_outbound(text, keyboard=keyboard)

    def send_spawn_alert(self, tier: str, rarity: str, location: str) -> None:
        if not self.enabled:
            return
        # Cooldown bucket includes location so two different spawn sites
        # of the same (tier, rarity) still both notify.
        if not self._can_notify(tier, rarity, f"@{location}"):
            self._on_log(f"[telegram] cooldown active, skipping spawn @{location}")
            return
        # The game's spawn toast does not include a specific card code,
        # so the closest "name" for a spawned droid is its type
        # description (e.g. "DIAMOND DROID"). Format mirrors the card
        # detection alert so both messages have the same shape.
        droid_name = f"{tier} DROID"
        text = (
            f"*Droid spawned!*\n"
            f"Name: `{droid_name}`\n"
            f"Tier: `{tier}`\n"
            f"Rarity: `{rarity}`"
        )
        self._enqueue_outbound(text, keyboard=None)

    def _post_message(self, text: str, _unused=None, *, keyboard: dict | None = None) -> None:
        try:
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            if keyboard is not None:
                payload["reply_markup"] = keyboard
            resp = requests.post(
                self._url("sendMessage"),
                json=payload,
                timeout=5,
            )
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and body.get("ok"):
                self._on_log("[telegram] alert sent")
            else:
                self._on_log(
                    f"[telegram] HTTP {resp.status_code}: "
                    f"{body.get('description') or body or resp.text[:200]}"
                )
        except Exception as e:
            self._on_log(f"[telegram] send failed: {e}")

    # ── inbound polling ──

    def start_polling(self) -> None:
        if not self.enabled:
            return
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        q = self._send_queue
        if q is not None:
            inserted = False
            for _ in range(_TELEGRAM_OUTBOUND_MAX_PENDING + 2):
                try:
                    q.put_nowait(_OUTBOUND_STOP)
                    inserted = True
                    break
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
            if not inserted:
                self._on_log("[telegram] could not enqueue shutdown sentinel; outbound may linger")
            t = self._outbound_thread
            self._outbound_thread = None
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                resp = requests.get(
                    self._url("getUpdates"),
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 2,
                        "allowed_updates": ["callback_query"],
                    },
                    timeout=8,
                )
                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    cq = update.get("callback_query")
                    if cq:
                        self._handle_callback(cq)
            except Exception:
                pass
            self._stop.wait(1.0)

    def _handle_callback(self, cq: dict) -> None:
        cid = cq.get("id", "")
        msg = cq.get("message", {}) or {}
        message_id = msg.get("message_id")
        chat_id = (msg.get("chat") or {}).get("id")
        data = cq.get("data", "")

        parts = data.split("|")
        if len(parts) != 4:
            self._answer_callback(cid, "Unknown payload")
            return
        action, tier, rarity, name = parts

        if action in ("confirm", "skip"):
            removed = self._store.mark_collected(tier, rarity, name)
            if action == "confirm":
                header = "Collected — removed from list"
                toast = "Removed from list"
                log_label = "confirmed"
            else:
                header = "Marked not needed — removed from list"
                toast = "Removed from list"
                log_label = "skipped"
            if removed:
                self._answer_callback(cid, toast)
                self._edit_message(
                    chat_id, message_id,
                    f"{header}\n"
                    f"Name: `{name}` | Tier: `{tier}` | Rarity: `{rarity}`",
                )
                self._on_log(f"[telegram] {log_label}: {name} / {tier} / {rarity}")
            else:
                self._answer_callback(cid, "Already removed")
        else:
            self._answer_callback(cid, "Unknown action")

    def _answer_callback(self, callback_id: str, text: str) -> None:
        if not callback_id:
            return
        try:
            requests.post(
                self._url("answerCallbackQuery"),
                json={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def _edit_message(self, chat_id, message_id, text: str) -> None:
        if not chat_id or not message_id:
            return
        try:
            requests.post(
                self._url("editMessageText"),
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=5,
            )
        except Exception:
            pass


# ─── Aggregate ───────────────────────────────────────────────────────────────

class Notifier:
    """Combines local + Telegram and applies the per-card cooldown for both.

    Spawn-toast notifications are intentionally disabled (see
    `config.ENABLE_SPAWN_NOTIFICATIONS`); only card-on-the-rack
    detections fire alerts. `spawn_detected` is preserved as a
    no-op-with-a-log-line so the matcher's spawn-detection path stays
    wired up for calibration and debugging without triggering a toast,
    sound, or Telegram message.
    """

    def __init__(
        self,
        store: TargetStore,
        telegram: TelegramBot | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._telegram = telegram
        self._on_log = on_log or (lambda _msg: None)
        self._local_cooldowns: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()
        # Sound + in-app log on hits; macOS toasts are gated by config.ENABLE_MACOS_TOAST.
        self.local_enabled = True
        self.telegram_enabled = telegram is not None and telegram.enabled

    def _can_notify_locally(self, tier: str, rarity: str, name: str) -> bool:
        key = (tier, rarity, name)
        now = time.perf_counter()
        retention = float(config.COOLDOWN_MAP_RETENTION_SECONDS)
        with self._lock:
            prune_stale_cooldown_entries(
                self._local_cooldowns,
                now,
                retention_seconds=retention,
            )
            last = self._local_cooldowns.get(key)
            if last is not None and (
                    now - last < config.COOLDOWN_NOTIFY_SECONDS):
                return False
            self._local_cooldowns[key] = now
            return True

    def card_detected(self, tier: str, rarity: str, name: str) -> None:
        tier = normalize(tier); rarity = normalize(rarity); name = normalize(name)
        if self.local_enabled and self._can_notify_locally(tier, rarity, name):
            if config.ENABLE_MACOS_TOAST:
                macos_notify(
                    title=f"Card: {name}",
                    subtitle=f"{tier} / {rarity}",
                    message="Press X on your DualSense to pick up",
                )
            play_alert_sound()
            self._on_log(f"[notify] {name} ({tier}/{rarity})")
        if self.telegram_enabled and self._telegram is not None:
            self._telegram.send_card_alert(tier, rarity, name)

    def spawn_detected(self, tier: str, rarity: str, location: str) -> None:
        # Spawn-toast notifications are intentionally disabled — only
        # card detections fire alerts. The [spawn] line logged by the
        # caller in __main__ already records that the toast was seen.
        if not config.ENABLE_SPAWN_NOTIFICATIONS:
            return

        tier = normalize(tier); rarity = normalize(rarity); location = normalize(location)
        cooldown_key = (tier, rarity, f"@{location}")
        if self.local_enabled and self._can_notify_locally(*cooldown_key):
            if config.ENABLE_MACOS_TOAST:
                macos_notify(
                    title=f"{rarity} {tier} spawned",
                    subtitle=location or f"{tier} DROID",
                    message=(
                        "Head to the location and grab the card"
                        if location else
                        "Head to a card station to find it"
                    ),
                )
            play_alert_sound()
            loc_log = f" @ {location}" if location else " (location truncated)"
            self._on_log(f"[notify] spawn {tier}/{rarity}{loc_log}")
        if self.telegram_enabled and self._telegram is not None:
            self._telegram.send_spawn_alert(tier, rarity, location)
