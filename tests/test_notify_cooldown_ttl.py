"""Unit tests for notify cooldown pruning helpers."""

from __future__ import annotations

import itertools
import threading
import time

import pytest

from mac_scanner.notify import Notifier, TelegramBot
from mac_scanner.notify_utils import prune_stale_cooldown_entries
from mac_scanner.targets import TargetStore


def test_prune_stale_removes_old_only() -> None:
    cool: dict[tuple[str, str, str], float] = {}
    keys = itertools.count()
    cool[("t", "r", next(keys))] = 1000.0
    cool[("t", "r", next(keys))] = 1180.0
    removed = prune_stale_cooldown_entries(
        cool,
        now=1250.0,
        retention_seconds=200.0,
    )
    assert removed == 1
    assert len(cool) == 1
    remaining = next(iter(cool.keys()))
    assert remaining[2] == 1


def test_notifier_pruning_frees_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_NOTIFY_SECONDS", 30.0,
        raising=False,
    )
    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_MAP_RETENTION_SECONDS", 1_000_000.0,
        raising=False,
    )

    notifier = Notifier(TargetStore())
    clock = {"now": 0.0}

    monkeypatch.setattr(time, "perf_counter", lambda: clock["now"])

    for i in range(10):
        clock["now"] = float(i * 45)
        assert notifier._can_notify_locally("A", "COMMON", f"K{i}") is True
    assert len(notifier._local_cooldowns) == 10

    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_MAP_RETENTION_SECONDS", 100.0,
        raising=False,
    )
    clock["now"] = 500.0
    assert notifier._can_notify_locally("A", "COMMON", "FRESH") is True
    assert len(notifier._local_cooldowns) <= 2


def test_telegram_cooldown_under_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_NOTIFY_SECONDS", 0.1,
        raising=False,
    )
    monkeypatch.setattr(
        "mac_scanner.config.COOLDOWN_MAP_RETENTION_SECONDS", 3600.0,
        raising=False,
    )

    bot = TelegramBot(TargetStore(), token="t", chat_id="1")
    errs: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(400):
                bot._can_notify("T", "R", "N")
                time.sleep(0.0005)
        except BaseException as e:
            errs.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bot.stop()

    assert not errs
    assert len(bot._cooldowns) == 1
