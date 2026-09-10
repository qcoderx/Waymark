from __future__ import annotations

import re
import unicodedata

from .domain import (
    ExtractedDirection,
    LandmarkPhrase,
    RelationType,
    RouteStep,
    SpatialRelation,
)


LANDMARK_TYPES = (
    "filling station",
    "petrol station",
    "bus stop",
    "roundabout",
    "transformer",
    "pharmacy",
    "junction",
    "mosque",
    "church",
    "market",
    "school",
    "estate",
    "hotel",
    "station",
    "bank",
    "shop",
    "gate",
)
KNOWN_NAMES = ("mobil", "firstbank", "gtbank", "access bank", "mama titi")
COLORS = {
    "black",
    "blue",
    "brown",
    "cream",
    "green",
    "grey",
    "orange",
    "pink",
    "purple",
    "red",
    "white",
    "yellow",
}
LEADING_NOISE = {
    "a",
    "an",
    "and",
    "after",
    "at",
    "by",
    "before",
    "beside",
    "for",
    "go",
    "look",
    "left",
    "na",
    "near",
    "opposite",
    "pass",
    "reach",
    "right",
    "see",
    "the",
    "then",
    "to",
    "towards",
    "when",
    "you",
}
ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _trim_phrase(value: str) -> str:
    words = normalize_name(value).split()
    while words and words[0] in LEADING_NOISE:
        words.pop(0)
    while words and words[-1] in {"and", "then", "small"}:
        words.pop()
    return " ".join(words[-4:])


def _landmark_type(name: str) -> str:
    for kind in LANDMARK_TYPES:
        if name.endswith(kind):
            return kind.replace(" ", "_")
    if name in KNOWN_NAMES:
        return "named_place"
    return "landmark"


class DirectionExtractor:
    """Conservative rules for route language common in Lagos delivery calls.

    The extractor only emits phrases present in the transcript. A model-assisted
    extractor can be composed later, but its output must pass the same evidence rule.
    """

    _typed_pattern = re.compile(
        rf"\b((?:[a-z0-9']+\s+){{0,4}}(?:{'|'.join(re.escape(x) for x in LANDMARK_TYPES)}))\b",
        re.IGNORECASE,
    )
    _known_pattern = re.compile(
        rf"\b({'|'.join(re.escape(x) for x in KNOWN_NAMES)})\b", re.IGNORECASE
    )
    _turn_pattern = re.compile(
        r"\b(?:(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+)?"
        r"(?:street|turn|road)?\s*(?:to\s+|by\s+)?(?:your\s+)?(left|right)\b",
        re.IGNORECASE,
    )
    _distance_pattern = re.compile(
        r"\b(?:about\s+|roughly\s+)?(\d{1,4})\s*(m|metres?|meters?|km|kilometres?)\b",
        re.IGNORECASE,
    )

    def extract(self, transcript: str, stt_confidence: float = 1.0) -> ExtractedDirection:
        clean = re.sub(r"\s+", " ", transcript).strip()
        lowered = normalize_name(clean)
        found: list[LandmarkPhrase] = []
        spans: list[tuple[int, int, str]] = []

        for match in self._typed_pattern.finditer(clean):
            phrase = _trim_phrase(match.group(1))
            if not phrase or phrase in {"left", "right"}:
                continue
            spans.append((match.start(), match.end(), phrase))

        for match in self._known_pattern.finditer(clean):
            phrase = normalize_name(match.group(1))
            if any(start <= match.start() < end for start, end, _ in spans):
                continue
            spans.append((match.start(), match.end(), phrase))

        seen: set[str] = set()
        for _, _, phrase in sorted(spans):
            normalized = normalize_name(phrase)
            if normalized in seen:
                continue
            seen.add(normalized)
            specificity = 0.92 if any(word in COLORS for word in normalized.split()) else 0.88
            found.append(
                LandmarkPhrase(
                    name=phrase.title() if phrase not in KNOWN_NAMES else phrase.capitalize(),
                    normalized_name=normalized,
                    landmark_type=_landmark_type(normalized),
                    confidence=max(0.35, min(0.98, specificity * stt_confidence)),
                )
            )

        relations: list[SpatialRelation] = []
        steps: list[RouteStep] = []

        pass_match = re.search(r"\b(pass|after|go past)\b", lowered)
        if pass_match and found:
            relation = RelationType.AFTER if pass_match.group(1) == "after" else RelationType.PASS
            first = found[0]
            instruction = f"Pass {first.name}"
            steps.append(
                RouteStep(
                    sequence=len(steps) + 1,
                    instruction=instruction,
                    relation_type=relation,
                    landmark_name=first.normalized_name,
                    confidence=first.confidence,
                )
            )
            relations.append(
                SpatialRelation(
                    relation_type=relation,
                    reference=first.normalized_name,
                    confidence=first.confidence,
                )
            )

        turn_match = self._turn_pattern.search(lowered)
        if turn_match:
            ordinal = ORDINALS.get(turn_match.group(1) or "")
            direction = turn_match.group(2).lower()
            relation = (
                RelationType.TURN_LEFT if direction == "left" else RelationType.TURN_RIGHT
            )
            label = (
                f"Take the {turn_match.group(1)} {direction}"
                if ordinal
                else f"Turn {direction}"
            )
            steps.append(
                RouteStep(
                    sequence=len(steps) + 1,
                    instruction=label,
                    relation_type=relation,
                    confidence=max(0.4, min(0.97, 0.94 * stt_confidence)),
                )
            )
            relations.append(
                SpatialRelation(
                    relation_type=relation,
                    ordinal=ordinal,
                    confidence=max(0.4, min(0.97, 0.94 * stt_confidence)),
                )
            )

        distance_match = self._distance_pattern.search(lowered)
        distance_meters: int | None = None
        if distance_match:
            distance_meters = int(distance_match.group(1))
            if distance_match.group(2).lower().startswith("k"):
                distance_meters *= 1000
        elif "pass am small" in lowered or "small distance" in lowered:
            distance_meters = 50

        if distance_meters is not None:
            steps.append(
                RouteStep(
                    sequence=len(steps) + 1,
                    instruction=(
                        "Continue a short distance"
                        if distance_meters == 50
                        else f"Continue for about {distance_meters} m"
                    ),
                    relation_type=RelationType.CONTINUE,
                    confidence=max(0.4, min(0.95, 0.9 * stt_confidence)),
                )
            )
            relations.append(
                SpatialRelation(
                    relation_type=RelationType.CONTINUE,
                    distance_meters=distance_meters,
                    confidence=max(0.4, min(0.95, 0.9 * stt_confidence)),
                )
            )

        relative = next(
            (
                (token, relation)
                for token, relation in (
                    ("opposite", RelationType.OPPOSITE),
                    ("beside", RelationType.BESIDE),
                    ("next to", RelationType.BESIDE),
                    ("before", RelationType.BEFORE),
                    ("near", RelationType.NEAR),
                )
                if token in lowered
            ),
            None,
        )
        if relative and len(found) >= 2:
            token, relation = relative
            subject, reference = found[-2], found[-1]
            steps.append(
                RouteStep(
                    sequence=len(steps) + 1,
                    instruction=f"Look for {subject.name}, {token} {reference.name}",
                    relation_type=relation,
                    landmark_name=subject.normalized_name,
                    confidence=min(subject.confidence, reference.confidence),
                )
            )
            relations.append(
                SpatialRelation(
                    relation_type=relation,
                    subject=subject.normalized_name,
                    reference=reference.normalized_name,
                    confidence=min(subject.confidence, reference.confidence),
                )
            )

        if not steps:
            for landmark in found:
                steps.append(
                    RouteStep(
                        sequence=len(steps) + 1,
                        instruction=f"Look for {landmark.name}",
                        relation_type=RelationType.NEAR,
                        landmark_name=landmark.normalized_name,
                        confidence=landmark.confidence,
                    )
                )

        evidence_scores = [item.confidence for item in found] + [
            item.confidence for item in relations
        ]
        confidence = sum(evidence_scores) / len(evidence_scores) if evidence_scores else 0.15
        return ExtractedDirection(
            raw_text=clean,
            landmarks=found,
            relations=relations,
            route_steps=steps,
            confidence=max(0.05, min(0.99, confidence)),
        )
