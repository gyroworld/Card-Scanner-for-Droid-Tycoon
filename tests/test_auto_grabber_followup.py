"""AutoGrabber.grab_now follow-up timing (pure logic; Quartz mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mac_scanner.input_sender import (
    AutoGrabConfig,
    AutoGrabber,
    POST_AUTO_GRAB_FOLLOWUP_DELAY_SECONDS,
    _POST_AUTO_GRAB_SYNTH_TAIL_SECONDS,
)


def test_grab_now_runs_burst_then_pause_then_send_f() -> None:
    seq: list[str] = []

    def fake_grab(*_a, **_k):
        seq.append("burst")
        return 42

    def fake_sleep(seconds: float) -> None:
        seq.append(f"sleep:{seconds}")

    fake_human = MagicMock()

    with patch(
        "mac_scanner.input_sender.auto_grab", side_effect=fake_grab
    ):
        with patch(
            "mac_scanner.input_sender.send_keypress", return_value=True
        ) as m_send:
            with patch(
                "mac_scanner.input_sender.time.sleep", side_effect=fake_sleep
            ):
                cfg = AutoGrabConfig(
                    key="e",
                    duration_seconds=0.01,
                    delay_seconds=0.01,
                    hold_seconds=0.03,
                    focus_remote_play=False,
                )
                grabber = AutoGrabber(cfg, human_monitor=fake_human)
                n = grabber.grab_now()

    assert n == 42
    assert seq == [
        "burst",
        f"sleep:{POST_AUTO_GRAB_FOLLOWUP_DELAY_SECONDS}",
    ]
    fake_human.mark_synth_window.assert_called_once_with(
        _POST_AUTO_GRAB_SYNTH_TAIL_SECONDS
    )
    m_send.assert_called_once_with(
        "f",
        focus_remote_play=False,
        hold_seconds=cfg.hold_seconds,
    )


def test_grab_now_without_human_monitor_still_sends_f() -> None:
    with patch("mac_scanner.input_sender.auto_grab", return_value=3):
        with patch(
            "mac_scanner.input_sender.send_keypress", return_value=True
        ) as m_send:
            with patch("mac_scanner.input_sender.time.sleep"):
                cfg = AutoGrabConfig(
                    key="e",
                    duration_seconds=0.01,
                    delay_seconds=0.01,
                    hold_seconds=0.05,
                    focus_remote_play=False,
                )
                AutoGrabber(cfg, human_monitor=None).grab_now()

    m_send.assert_called_once_with(
        "f",
        focus_remote_play=False,
        hold_seconds=cfg.hold_seconds,
    )
