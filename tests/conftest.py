"""Pytest configuration and shared markers."""

from __future__ import annotations

import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "macos_only: requires Darwin",
    )
    config.addinivalue_line(
        "markers",
        "needs_vision: requires Vision framework",
    )


@pytest.fixture
def not_darwin_skip() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS only")
