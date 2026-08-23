"""Small helpers shared by notifier paths (testable without I/O)."""

from __future__ import annotations


def prune_stale_cooldown_entries(
    cooldowns: dict[tuple[str, str, str], float],
    now: float,
    *,
    retention_seconds: float,
) -> int:
    """Remove keys whose last-notify timestamp is older than retention window.

    Returns the number of keys removed.
    """
    if retention_seconds <= 0:
        return 0
    cutoff = now - retention_seconds
    stale = [k for k, t in cooldowns.items() if t < cutoff]
    for k in stale:
        del cooldowns[k]
    return len(stale)
