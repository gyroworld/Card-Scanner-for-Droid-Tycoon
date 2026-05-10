"""Match OCR output against the active target store.

Algorithm (spatial):

  1. Classify each OCR observation:
       - "spawn" if it looks like a spawn-toast line (handled separately
         by find_spawn_event).
       - Otherwise, decompose its tokens into TIER words, RARITY words,
         and residual name words. Tokens like "DIAMONDRARE" are split
         into ("DIAMOND", "RARE") since Vision sometimes joins them.
       - Build a "label" with whatever tier/rarity it carries plus the
         residual name tokens, if any.

  2. Combine partial labels (a tier-only obs + an adjacent rarity-only
     obs on the same row) into "complete labels". Vision usually emits
     "DEFAULT COMMON" as a single observation, but on lower-quality
     frames it splits them. We pair them when their bboxes are close in
     y and adjacent in x.

  3. For each complete label, find the name observation directly ABOVE
     it (smaller y) within a similar x-range. That's the card the label
     describes. Vision returns observations roughly in reading order, so
     the right pairing is also usually the spatially-closest one above.

  4. Fuzzy-match each (name, tier, rarity) candidate against the target
     store bucket using RapidFuzz WRatio. WRatio handles truncation
     ("MONO-WLKR" for "MONO-WALKER") and OCR substitutions. Return all
     matches above the threshold; the caller deduplicates with cooldowns.

When no bboxes are available (older OCR output or test fixtures), we
fall back to a non-spatial mode: every (tier, rarity) seen in the frame
is paired against every candidate. This is the previous behaviour and
keeps single-card test fixtures working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from . import config
from .ocr import OCRResult
from .targets import TargetStore, normalize


@dataclass(frozen=True)
class Match:
    tier: str
    rarity: str
    name: str
    score: float


@dataclass(frozen=True)
class SpawnEvent:
    """A spawn-announcement toast like
    'A DIAMOND DROID (RARE) SPAWNED AT THE SANDCRAWLER'.
    """
    tier: str
    rarity: str
    location: str
    raw: str


def _spawn_pattern_strict() -> re.Pattern[str]:
    tiers = "|".join(re.escape(normalize(t)) for t in config.TIERS)
    rarities = "|".join(re.escape(normalize(r)) for r in config.RARITIES)
    # OCR feeds in already-normalized strings (uppercase, parens stripped).
    # The leading "A" is optional because OCR sometimes substitutes a
    # period or other glyph for it (e.g. ". DIAMOND DROID RARE SPAWNED ...").
    # `S\w*WNED` tolerates the common "SOAWNED" misread we see in dumps.
    return re.compile(
        rf"(?:\bA\s+)?(?P<tier>{tiers})\s+DROID\s+(?P<rarity>{rarities})\s+"
        rf"S\w*WNED\s+AT\s+(?:THE\s+)?(?P<loc>[A-Z0-9][A-Z0-9 \-]*?)\s*$"
    )


def _spawn_pattern_lenient() -> re.Pattern[str]:
    """Fallback used when the spawn toast is truncated or partly occluded
    by the player character. Drops the "SPAWNED AT <LOC>" requirement and
    just requires the distinctive "<TIER> DROID <RARITY>" trio. The trio
    only ever appears in spawn announcements (card racks display the tier
    and rarity on a separate line from the card name; nothing else in the
    HUD chains them like this), so it's a safe fallback."""
    tiers = "|".join(re.escape(normalize(t)) for t in config.TIERS)
    rarities = "|".join(re.escape(normalize(r)) for r in config.RARITIES)
    return re.compile(
        rf"(?P<tier>{tiers})\s+DROID\s*\(?\s*(?P<rarity>{rarities})\)?"
    )


_SPAWN_RE = _spawn_pattern_strict()
_SPAWN_RE_LENIENT = _spawn_pattern_lenient()
_TIERS_NORM = frozenset(normalize(t) for t in config.TIERS)


def _looks_like_spawn(text_norm: str) -> bool:
    """Cheap pre-check used to exclude toast lines from card-name candidates.

    A spawn toast always contains "<TIER> DROID" — that pattern is the
    unique fingerprint. Inventory/HUD strings like 'MECHA-DROID' don't
    match because no tier word precedes them.
    """
    if "DROID" not in text_norm:
        return False
    padded = " " + text_norm + " "
    for tier in _TIERS_NORM:
        if f" {tier} DROID" in padded:
            return True
    return False


def find_spawn_event(
    ocr_results: list[OCRResult],
    *,
    min_confidence: float = config.OCR_MIN_CONFIDENCE,
) -> SpawnEvent | None:
    for r in ocr_results:
        if r.confidence < min_confidence:
            continue
        norm = normalize(r.text)
        if not _looks_like_spawn(norm):
            continue
        m = _SPAWN_RE.search(norm)
        if m:
            return SpawnEvent(
                tier=m.group("tier"),
                rarity=m.group("rarity"),
                location=m.group("loc").strip(),
                raw=r.text,
            )
        m = _SPAWN_RE_LENIENT.search(norm)
        if m:
            return SpawnEvent(
                tier=m.group("tier"),
                rarity=m.group("rarity"),
                location="",  # truncated/occluded by player character
                raw=r.text,
            )
    return None


# ─── Token decomposition ────────────────────────────────────────────────────

def _decompose_token(
    token: str,
    tiers: set[str],
    rarities: set[str],
) -> tuple[set[str], set[str], list[str]]:
    """Return (tier_words, rarity_words, name_words) for a single token.

    Handles concatenated cases ("DIAMONDRARE" -> {"DIAMOND"}, {"RARE"}).
    """
    if not token:
        return set(), set(), []
    if token in tiers:
        return {token}, set(), []
    if token in rarities:
        return set(), {token}, []
    for tier in tiers:
        if token.startswith(tier):
            rest = token[len(tier):].lstrip("-_ ")
            if rest in rarities:
                return {tier}, {rest}, []
    for rarity in rarities:
        if token.endswith(rarity) and len(token) > len(rarity):
            prefix = token[: -len(rarity)].rstrip("-_ ")
            if prefix in tiers:
                return {prefix}, {rarity}, []
    return set(), set(), [token]


@dataclass
class _Obs:
    """An OCR observation parsed into label + name parts."""
    raw_text: str        # original text
    norm: str            # normalized
    confidence: float
    bbox: tuple[float, float, float, float] | None  # left, top, w, h
    tiers: set[str] = field(default_factory=set)
    rarities: set[str] = field(default_factory=set)
    name_words: list[str] = field(default_factory=list)
    is_spawn: bool = False

    @property
    def name_text(self) -> str:
        return " ".join(self.name_words).strip()

    @property
    def has_label(self) -> bool:
        return bool(self.tiers) or bool(self.rarities)

    @property
    def is_pure_label(self) -> bool:
        return self.has_label and not self.name_words

    @property
    def cx(self) -> float:
        if self.bbox is None:
            return 0.0
        return self.bbox[0] + self.bbox[2] / 2

    @property
    def cy(self) -> float:
        if self.bbox is None:
            return 0.0
        return self.bbox[1] + self.bbox[3] / 2

    @property
    def left(self) -> float:
        return self.bbox[0] if self.bbox else 0.0

    @property
    def right(self) -> float:
        return (self.bbox[0] + self.bbox[2]) if self.bbox else 0.0

    @property
    def top(self) -> float:
        return self.bbox[1] if self.bbox else 0.0

    @property
    def height(self) -> float:
        return self.bbox[3] if self.bbox else 0.0


def _classify(
    ocr_results: list[OCRResult],
    *,
    min_confidence: float,
    tiers: set[str],
    rarities: set[str],
) -> list[_Obs]:
    obs_list: list[_Obs] = []
    for r in ocr_results:
        if r.confidence < min_confidence:
            continue
        norm = normalize(r.text)
        if not norm:
            continue
        is_spawn = _looks_like_spawn(norm)
        ob = _Obs(
            raw_text=r.text,
            norm=norm,
            confidence=r.confidence,
            bbox=r.bbox,
            is_spawn=is_spawn,
        )
        for token in norm.split():
            t_words, r_words, n_words = _decompose_token(token, tiers, rarities)
            ob.tiers |= t_words
            ob.rarities |= r_words
            ob.name_words.extend(n_words)
        obs_list.append(ob)
    return obs_list


# ─── Spatial pairing ────────────────────────────────────────────────────────

def _x_overlap(a: _Obs, b: _Obs) -> float:
    if a.bbox is None or b.bbox is None:
        return 0.0
    return max(0.0, min(a.right, b.right) - max(a.left, b.left))


def _combine_partial_labels(labels: list[_Obs]) -> list[_Obs]:
    """Pair tier-only and rarity-only obs that look like the same physical
    label split across two observations (similar y, adjacent x)."""
    complete = [lab for lab in labels if lab.tiers and lab.rarities]
    tier_only = [lab for lab in labels if lab.tiers and not lab.rarities]
    rarity_only = [lab for lab in labels if lab.rarities and not lab.tiers]

    if not tier_only or not rarity_only:
        return complete + tier_only + rarity_only

    used_t: set[int] = set()
    used_r: set[int] = set()
    paired: list[_Obs] = []
    for i, t in enumerate(tier_only):
        if t.bbox is None:
            continue
        best_j = None
        best_dx = float("inf")
        for j, rlab in enumerate(rarity_only):
            if j in used_r or rlab.bbox is None:
                continue
            dy = abs(t.cy - rlab.cy)
            if dy > max(t.height, rlab.height) * 0.6:
                continue
            dx = abs(t.cx - rlab.cx)
            avg_w = (t.bbox[2] + rlab.bbox[2]) / 2
            if dx > avg_w * 6:
                continue
            if dx < best_dx:
                best_dx = dx
                best_j = j
        if best_j is None:
            continue
        rlab = rarity_only[best_j]
        used_t.add(i)
        used_r.add(best_j)
        new_left = min(t.left, rlab.left)
        new_top = min(t.top, rlab.top)
        new_right = max(t.right, rlab.right)
        new_bottom = max(t.top + t.height, rlab.top + rlab.height)
        merged = _Obs(
            raw_text=f"{t.raw_text} {rlab.raw_text}",
            norm=f"{t.norm} {rlab.norm}",
            confidence=min(t.confidence, rlab.confidence),
            bbox=(new_left, new_top, new_right - new_left, new_bottom - new_top),
            tiers=t.tiers | rlab.tiers,
            rarities=t.rarities | rlab.rarities,
        )
        paired.append(merged)

    leftover_t = [t for i, t in enumerate(tier_only) if i not in used_t]
    leftover_r = [rlab for j, rlab in enumerate(rarity_only) if j not in used_r]
    return complete + paired + leftover_t + leftover_r


def _find_name_above(
    label: _Obs,
    candidates: list[_Obs],
) -> _Obs | None:
    """Pick the candidate observation most likely to be the card-name
    text for `label`: directly above it, with overlapping x-range."""
    if label.bbox is None:
        return None
    best: _Obs | None = None
    best_dy = float("inf")
    label_w = label.bbox[2] or 1.0
    for cand in candidates:
        if cand.bbox is None:
            continue
        if cand.top >= label.top:
            continue
        if not cand.name_words:
            continue
        overlap = _x_overlap(label, cand)
        cand_w = cand.bbox[2] or 1.0
        if overlap < 0.25 * min(label_w, cand_w):
            cx_dist = abs(label.cx - cand.cx)
            if cx_dist > 0.6 * label_w:
                continue
        dy = label.top - (cand.top + cand.height)
        if dy < 0:
            dy = label.top - cand.top
        if dy > 4 * label.height and dy > 80:
            continue
        if dy < best_dy:
            best_dy = dy
            best = cand
    return best


# ─── Public API ─────────────────────────────────────────────────────────────

@dataclass
class Trace:
    """Diagnostic snapshot of a single parse() invocation."""
    tiers_seen: set[str] = field(default_factory=set)
    rarities_seen: set[str] = field(default_factory=set)
    candidate_texts: list[str] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    best_any: Match | None = None
    threshold: int = config.FUZZY_SCORE_THRESHOLD
    has_active_bucket: bool = False
    spatial_mode: bool = False
    pairings: list[tuple[str, str, str]] = field(default_factory=list)
    # ^ list of (name_text, tier, rarity) triples that survived spatial pairing.

    @property
    def best_above_threshold(self) -> Match | None:
        if not self.matches:
            return None
        return max(self.matches, key=lambda m: m.score)

    def as_log_str(self) -> str:
        if self.matches:
            parts = [f"{m.name}@{m.score:.0f} ({m.tier}/{m.rarity})"
                     for m in sorted(self.matches, key=lambda m: -m.score)]
            return f"hits={len(self.matches)} " + ", ".join(parts)
        bits: list[str] = []
        bits.append(f"tiers={sorted(self.tiers_seen) or '-'}")
        bits.append(f"rarities={sorted(self.rarities_seen) or '-'}")
        bits.append(f"candidates={len(self.candidate_texts)}")
        bits.append(f"pairings={len(self.pairings)}")
        if self.best_any is not None:
            m = self.best_any
            bits.append(f"best={m.name}@{m.score:.0f} ({m.tier}/{m.rarity})")
            bits.append(f"threshold={self.threshold}")
        elif self.tiers_seen and self.rarities_seen and not self.has_active_bucket:
            bits.append("no-active-bucket-for-pair")
        return " ".join(bits)


def _score_against_bucket(
    text: str,
    bucket: list[str],
) -> tuple[str, float] | None:
    if not bucket or not text:
        return None
    hit = process.extractOne(text, bucket, scorer=fuzz.WRatio)
    if hit is None:
        return None
    name, score, _idx = hit
    return name, float(score)


def parse_with_trace(
    ocr_results: list[OCRResult],
    store: TargetStore,
    *,
    min_confidence: float = config.OCR_MIN_CONFIDENCE,
    fuzzy_threshold: int = config.FUZZY_SCORE_THRESHOLD,
) -> Trace:
    trace = Trace(threshold=fuzzy_threshold)
    tiers_norm = {normalize(t) for t in config.TIERS}
    rarities_norm = {normalize(r) for r in config.RARITIES}

    obs_list = _classify(
        ocr_results,
        min_confidence=min_confidence,
        tiers=tiers_norm,
        rarities=rarities_norm,
    )

    label_obs = [o for o in obs_list if o.is_pure_label and not o.is_spawn]
    name_obs = [o for o in obs_list
                if o.name_words and not o.is_spawn]

    for o in label_obs:
        trace.tiers_seen |= o.tiers
        trace.rarities_seen |= o.rarities
    for o in obs_list:
        if o.has_label and o.name_words and not o.is_spawn:
            trace.tiers_seen |= o.tiers
            trace.rarities_seen |= o.rarities
        if o.name_words and not o.is_spawn:
            trace.candidate_texts.append(o.name_text)

    have_bboxes = all(o.bbox is not None for o in obs_list) and bool(obs_list)
    trace.spatial_mode = have_bboxes

    if have_bboxes:
        complete_labels = _combine_partial_labels(label_obs)
        complete_labels = [
            lab for lab in complete_labels if lab.tiers and lab.rarities
        ]
        for lab in complete_labels:
            paired_name = _find_name_above(lab, name_obs)
            for tier in lab.tiers:
                for rarity in lab.rarities:
                    if not store.has_bucket(tier, rarity):
                        continue
                    trace.has_active_bucket = True
                    bucket = list(store.bucket(tier, rarity))
                    if not bucket:
                        continue
                    candidate_pool: list[str] = []
                    if paired_name and paired_name.name_text:
                        candidate_pool.append(paired_name.name_text)
                        trace.pairings.append(
                            (paired_name.name_text, tier, rarity)
                        )
                    for text in candidate_pool:
                        scored = _score_against_bucket(text, bucket)
                        if scored is None:
                            continue
                        name, score = scored
                        cand = Match(tier=tier, rarity=rarity,
                                     name=name, score=score)
                        if (trace.best_any is None
                                or cand.score > trace.best_any.score):
                            trace.best_any = cand
                        if score >= fuzzy_threshold:
                            trace.matches.append(cand)
        return trace

    # Fallback: no bbox information available. Use the previous (tier x
    # rarity x candidate) sweep so single-image fixtures still match.
    if (not trace.tiers_seen
            or not trace.rarities_seen
            or not trace.candidate_texts):
        return trace
    for tier in trace.tiers_seen:
        for rarity in trace.rarities_seen:
            if not store.has_bucket(tier, rarity):
                continue
            trace.has_active_bucket = True
            bucket = list(store.bucket(tier, rarity))
            if not bucket:
                continue
            for text in trace.candidate_texts:
                scored = _score_against_bucket(text, bucket)
                if scored is None:
                    continue
                name, score = scored
                cand = Match(tier=tier, rarity=rarity, name=name, score=score)
                if (trace.best_any is None
                        or cand.score > trace.best_any.score):
                    trace.best_any = cand
                if score >= fuzzy_threshold:
                    trace.matches.append(cand)
    return trace


def parse(
    ocr_results: list[OCRResult],
    store: TargetStore,
    *,
    min_confidence: float = config.OCR_MIN_CONFIDENCE,
    fuzzy_threshold: int = config.FUZZY_SCORE_THRESHOLD,
) -> Match | None:
    return parse_with_trace(
        ocr_results, store,
        min_confidence=min_confidence,
        fuzzy_threshold=fuzzy_threshold,
    ).best_above_threshold
