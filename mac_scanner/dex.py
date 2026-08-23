"""Local Droidex catalog loader and watchlist builder.

The catalog lives in ``data/droid_dex.json`` (refreshed from the wiki via
``scripts/refresh_droid_dex.py``). ``config`` imports the vocabularies and
``TARGETS_INITIAL`` from here so OCR matching and alerts stay in sync with
the published droid list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEX_PATH = _REPO_ROOT / "data" / "droid_dex.json"


class DexError(RuntimeError):
    """Raised when the on-disk Droidex catalog is missing or invalid."""


def load_dex(path: Path | None = None) -> dict[str, Any]:
    dex_path = path or DEFAULT_DEX_PATH
    try:
        raw = json.loads(dex_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DexError(
            f"Droidex catalog not found at {dex_path}. "
            "Run scripts/refresh_droid_dex.py or restore data/droid_dex.json."
        ) from exc
    except json.JSONDecodeError as exc:
        raise DexError(f"Droidex catalog is not valid JSON: {dex_path}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("droids"), list):
        raise DexError(f"Droidex catalog missing droids list: {dex_path}")
    return raw


def _norm_set(values: list[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(str(v).upper() for v in values)


def _norm_token(value: str) -> str:
    return str(value).strip().upper()


def card_names(dex: dict[str, Any]) -> frozenset[str]:
    """Canonical OCR card codes from the catalog."""
    names: set[str] = set()
    for droid in dex["droids"]:
        name = str(droid.get("name", "")).strip().upper()
        if name:
            names.add(name)
    return frozenset(names)


def ocr_vocab_names(dex: dict[str, Any]) -> frozenset[str]:
    """Canonical names plus aliases — for Vision ``customWords`` bias."""
    names = set(card_names(dex))
    for droid in dex["droids"]:
        for alias in droid.get("aliases") or []:
            text = str(alias).strip().upper()
            if text:
                names.add(text)
    return frozenset(names)


def tiers(dex: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(t).upper() for t in dex.get("tiers") or [])


def rarities(dex: dict[str, Any]) -> frozenset[str]:
    return frozenset(str(r).upper() for r in dex.get("rarities") or [])


def build_targets(
    dex: dict[str, Any],
    watch: dict[str, Any] | None = None,
) -> dict[str, dict[str, frozenset[str]]]:
    """Build ``TARGETS_INITIAL`` from the dex ``watch`` filter.

    Watch keys:
      - tiers: list[str] (required non-empty)
      - rarities: list[str] (required non-empty)
      - classes: list[str] | null  (null = all classes)
      - names: list[str] | null    (null = all names matching other filters)
      - exclude_names: list[str]   (always removed)
    """
    rules = watch if watch is not None else dex.get("watch") or {}
    watch_tiers = _norm_set(rules.get("tiers"))
    watch_rarities = _norm_set(rules.get("rarities"))
    watch_classes = _norm_set(rules.get("classes"))
    watch_names = _norm_set(rules.get("names"))
    exclude = _norm_set(rules.get("exclude_names") or []) or frozenset()

    if not watch_tiers or not watch_rarities:
        raise DexError(
            "Droidex watch.tiers and watch.rarities must be non-empty lists"
        )

    # name -> set of paint variants it can appear as
    by_name: dict[str, dict[str, Any]] = {}
    for droid in dex["droids"]:
        name = str(droid.get("name", "")).strip().upper()
        if not name:
            continue
        dclass = str(droid.get("class", "")).strip()
        rarity = str(droid.get("rarity", "")).strip().upper()
        variants = [
            str(v).upper() for v in (droid.get("variants") or []) if str(v).strip()
        ]
        if name in exclude:
            continue
        if watch_classes is not None and _norm_token(dclass) not in watch_classes:
            continue
        if _norm_token(rarity) not in watch_rarities:
            continue
        if watch_names is not None and name not in watch_names:
            continue
        by_name[name] = {
            "rarity": _norm_token(rarity),
            "variants": variants,
            "aliases": [
                str(a).strip().upper()
                for a in (droid.get("aliases") or [])
                if str(a).strip()
            ],
        }

    targets: dict[str, dict[str, set[str]]] = {}
    for name, meta in by_name.items():
        rarity = meta["rarity"]
        # A droid's catalog rarity is intrinsic (Mouse is Common, BB9 is
        # Legendary). Watch rarities select which intrinsic rarities we care
        # about; watch tiers select which paint variants of those droids.
        #
        # Iconic (and any future base-only) droids only list DEFAULT. If the
        # watch tiers are high paints only, still include the droid under the
        # variants it actually has so it is not silently dropped.
        names_for_bucket = {name, *meta["aliases"]}
        variant_set = set(meta["variants"])
        selected_tiers = [t for t in watch_tiers if t in variant_set] if variant_set else list(watch_tiers)
        if not selected_tiers and variant_set:
            selected_tiers = sorted(variant_set)
        for tier in selected_tiers:
            targets.setdefault(tier, {}).setdefault(rarity, set()).update(
                names_for_bucket
            )

    return {
        tier: {rarity: frozenset(names) for rarity, names in by_rarity.items()}
        for tier, by_rarity in targets.items()
    }


def summarize_targets(
    targets: dict[str, dict[str, frozenset[str]]],
) -> str:
    buckets = sum(len(r) for r in targets.values())
    names = len({n for by_r in targets.values() for s in by_r.values() for n in s})
    return f"{buckets} buckets, {names} distinct names"
