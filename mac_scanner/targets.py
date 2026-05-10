"""Target state — the in-memory dictionary of (tier, rarity, name) triples
we still want to be notified about, plus persistence of confirmed pickups.

Persistence schema (collected_targets.json) is a list of triples:
    [["RAINBOW", "LEGENDARY", "BB9"], ...]
Each triple is permanently removed from the active set on the next launch.
"""

from __future__ import annotations

import json
import threading
import unicodedata
from pathlib import Path

from . import config


_BRACKET_TRANSLATE = str.maketrans({c: " " for c in "()[]{}<>"})


def normalize(text: str) -> str:
    """Strip diacritics, replace bracket punctuation with whitespace,
    uppercase, then collapse whitespace.

    Hyphens and other in-name punctuation are kept (e.g. ``MONO-WALKER``
    must stay one token), but ``(RARE)`` collapses to ``RARE`` so it
    matches the bare rarity vocabulary.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    no_marks = "".join(c for c in nfkd if not unicodedata.combining(c))
    debr = no_marks.translate(_BRACKET_TRANSLATE)
    return " ".join(debr.upper().split())


class TargetStore:
    """Thread-safe nested {tier: {rarity: {names}}} store."""

    def __init__(self, initial: dict[str, dict[str, frozenset[str]]] | None = None,
                 collected_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        seed = initial if initial is not None else config.TARGETS_INITIAL
        self._data: dict[str, dict[str, set[str]]] = {
            normalize(tier): {
                normalize(rarity): {normalize(n) for n in names}
                for rarity, names in by_rarity.items()
            }
            for tier, by_rarity in seed.items()
        }
        self._collected_path = collected_path or config.COLLECTED_FILE
        self._already_collected: set[tuple[str, str, str]] = set()

    def load_collected(self) -> int:
        """Apply previously-confirmed pickups from disk. Returns count loaded."""
        if not self._collected_path.exists():
            return 0
        try:
            with self._collected_path.open("r", encoding="utf-8") as f:
                triples = json.load(f)
        except (OSError, json.JSONDecodeError):
            return 0
        count = 0
        for entry in triples:
            if not (isinstance(entry, list) and len(entry) == 3):
                continue
            t, r, n = (normalize(s) for s in entry)
            with self._lock:
                self._already_collected.add((t, r, n))
                self._discard_locked(t, r, n)
            count += 1
        return count

    def _discard_locked(self, tier: str, rarity: str, name: str) -> None:
        if tier in self._data and rarity in self._data[tier]:
            self._data[tier][rarity].discard(name)
            if not self._data[tier][rarity]:
                del self._data[tier][rarity]
            if not self._data[tier]:
                del self._data[tier]

    def has_bucket(self, tier: str, rarity: str) -> bool:
        with self._lock:
            return tier in self._data and bool(self._data[tier].get(rarity))

    def bucket(self, tier: str, rarity: str) -> list[str]:
        with self._lock:
            return list(self._data.get(tier, {}).get(rarity, ()))

    def is_active(self, tier: str, rarity: str, name: str) -> bool:
        t, r, n = normalize(tier), normalize(rarity), normalize(name)
        with self._lock:
            return t in self._data and r in self._data[t] and n in self._data[t][r]

    def mark_collected(self, tier: str, rarity: str, name: str) -> bool:
        """Remove triple from active set and persist. Returns True on first removal."""
        t, r, n = normalize(tier), normalize(rarity), normalize(name)
        with self._lock:
            if (t, r, n) in self._already_collected:
                return False
            self._already_collected.add((t, r, n))
            self._discard_locked(t, r, n)
        self._persist()
        return True

    def _persist(self) -> None:
        with self._lock:
            triples = sorted(list(t) for t in self._already_collected)
        try:
            self._collected_path.write_text(
                json.dumps(triples, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def snapshot(self) -> dict[str, dict[str, list[str]]]:
        """Defensive copy for diagnostics."""
        with self._lock:
            return {
                t: {r: sorted(names) for r, names in by_r.items()}
                for t, by_r in self._data.items()
            }
