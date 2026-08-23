"""Tests for the local Droidex catalog loader / watchlist builder."""

from __future__ import annotations

import json
from pathlib import Path

from mac_scanner import config, dex


def test_dex_file_loads_and_has_expected_shape() -> None:
    catalog = dex.load_dex()
    assert catalog["tiers"]
    assert catalog["rarities"]
    assert catalog["classes"] == ["Worker", "Astromech", "Battle"]
    assert len(catalog["droids"]) >= 70
    names = {d["name"] for d in catalog["droids"]}
    # Pre-update staples
    assert "BB9" in names
    assert "PROTO-ROLLER" in names
    # Post-update mythic / iconic / fusion
    assert "SNOW MOUSE" in names
    assert "R2-D2" in names
    assert "WHL-EX" in names


def test_config_vocab_comes_from_dex() -> None:
    assert "BESKAR" in config.TIERS
    assert "STELLAR" in config.TIERS
    assert "MYTHIC" in config.RARITIES
    assert "ICONIC" in config.RARITIES
    assert "MONO-WLKR" in config.ALL_CARD_NAMES
    assert "MONO-WALKER" in config.OCR_CARD_NAMES  # legacy alias
    assert "B-U4D" in config.ALL_CARD_NAMES
    assert "BU-4D" in config.OCR_CARD_NAMES


def test_build_targets_respects_class_and_rarity_filters() -> None:
    catalog = dex.load_dex()
    targets = dex.build_targets(
        catalog,
        watch={
            "tiers": ["RAINBOW"],
            "rarities": ["LEGENDARY"],
            "classes": ["Astromech"],
            "names": None,
            "exclude_names": [],
        },
    )
    assert set(targets) == {"RAINBOW"}
    assert set(targets["RAINBOW"]) == {"LEGENDARY"}
    names = targets["RAINBOW"]["LEGENDARY"]
    assert "BB9" in names
    assert "R7" in names
    assert "PROTO-ROLLER" not in names  # Worker legendary
    assert "QIK-BIT" in names  # fusion astromech legendary


def test_iconic_base_only_still_watched() -> None:
    catalog = dex.load_dex()
    targets = dex.build_targets(
        catalog,
        watch={
            "tiers": ["RAINBOW", "STELLAR"],
            "rarities": ["ICONIC"],
            "classes": None,
            "names": None,
            "exclude_names": [],
        },
    )
    # Iconics only exist as DEFAULT on the Droidex; watch should fall back.
    assert "DEFAULT" in targets
    assert "R2-D2" in targets["DEFAULT"]["ICONIC"]
    assert "RAINBOW" not in targets


def test_exclude_names() -> None:
    catalog = dex.load_dex()
    targets = dex.build_targets(
        catalog,
        watch={
            "tiers": ["RAINBOW"],
            "rarities": ["LEGENDARY"],
            "classes": None,
            "names": None,
            "exclude_names": ["BB9"],
        },
    )
    assert "BB9" not in targets["RAINBOW"]["LEGENDARY"]


def test_refresh_script_exists() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "refresh_droid_dex.py"
    assert script.is_file()
    # Sanity: committed dex is valid JSON with a watch block.
    raw = json.loads(dex.DEFAULT_DEX_PATH.read_text(encoding="utf-8"))
    assert "watch" in raw
