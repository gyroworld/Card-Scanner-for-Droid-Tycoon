"""Telegram outbound queue (single worker, bounded backlog)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from mac_scanner.notify import TelegramBot
from mac_scanner.targets import TargetStore


@pytest.fixture(autouse=True)
def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)


def test_outbound_worker_processes_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_NOTIFY_SECONDS", 0.0,
        raising=False,
    )

    urls: list[str] = []

    def fake_post(url: str, **kwargs: object):
        urls.append(url)
        r = MagicMock()
        r.status_code = 200
        r.text = "{}"
        r.json.return_value = {"ok": True}
        return r

    monkeypatch.setattr("mac_scanner.notify.requests.post", fake_post)

    bot = TelegramBot(TargetStore(), token="tok", chat_id="999")
    try:
        bot.send_card_alert("GOLD", "RARE", "BB9")
        bot.send_spawn_alert("DIAMOND", "EPIC", "TOWN")
        deadline = time.perf_counter() + 5.0
        while bot._send_queue is not None and bot._send_queue.qsize() > 0:
            threading.Event().wait(timeout=0.02)
            if time.perf_counter() > deadline:
                break
        threading.Event().wait(timeout=2.5)
        assert urls
        assert any("sendMessage" in u for u in urls)
    finally:
        bot.stop()


def test_burst_uses_single_outbound_named_thread() -> None:
    before = sum(
        1 for t in threading.enumerate() if t.name == "mac_scanner.telegram_outbound"
    )
    bot = TelegramBot(TargetStore(), token="t", chat_id="1")
    try:
        after = sum(
            1 for t in threading.enumerate() if t.name == "mac_scanner.telegram_outbound"
        )
        assert after == before + 1
        for i in range(40):
            bot._enqueue_outbound(f"*M{i}*", keyboard=None)
    finally:
        bot.stop()


def test_queue_full_logs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_NOTIFY_SECONDS", 0.0,
        raising=False,
    )

    def slow_post(url: str, **_kwargs):
        time.sleep(0.2)
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"ok": True}
        return r

    monkeypatch.setattr("mac_scanner.notify.requests.post", slow_post)
    msgs: list[str] = []

    bot = TelegramBot(TargetStore(), token="t", chat_id="1", on_log=msgs.append)
    try:
        for i in range(50):
            bot._enqueue_outbound(f"M{i}", keyboard=None)
        full_msgs = [m for m in msgs if "outbound queue full" in m]
        assert len(full_msgs) >= 1
        assert msgs.count(full_msgs[0]) == 1
    finally:
        bot.stop()
