from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import httpx

from .config import Settings
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


ROUTE_INTERPRETATION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "delivery_guidance",
                "arrival_confirmation",
                "conversation_only",
                "clarification_needed",
            ],
        },
        "landmarks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "landmark_type": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "landmark_type", "evidence_quote", "confidence"],
                "additionalProperties": False,
            },
        },
        "route_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string"},
                    "relation_type": {
                        "type": "string",
                        "enum": [item.value for item in RelationType],
                    },
                    "landmark_name": {"type": ["string", "null"]},
                    "reference_landmark_name": {"type": ["string", "null"]},
                    "distance_meters": {"type": ["integer", "null"]},
                    "ordinal": {"type": ["integer", "null"]},
                    "evidence_quote": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "instruction",
                    "relation_type",
                    "landmark_name",
                    "reference_landmark_name",
                    "distance_meters",
                    "ordinal",
                    "evidence_quote",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
        "needs_clarification": {"type": "boolean"},
    },
    "required": [
        "intent",
        "landmarks",
        "route_steps",
        "confidence",
        "needs_clarification",
    ],
    "additionalProperties": False,
}


class ConversationDirectionPlanner:
    """Interpret a rolling two-party transcript and return the current best route."""

    def __init__(self, settings: Settings, fallback: DirectionExtractor | None = None) -> None:
        self.settings = settings
        self.fallback = fallback or DirectionExtractor()

    async def extract(
        self,
        turns: list[dict[str, Any]],
        *,
        coarse_address: str | None = None,
    ) -> ExtractedDirection:
        clean_turns = [
            {
                "speaker": str(turn.get("speaker", "unknown")),
                "text": re.sub(r"\s+", " ", str(turn.get("transcript", ""))).strip(),
                "stt_confidence": float(turn.get("confidence", 1.0)),
            }
            for turn in turns[-10:]
            if str(turn.get("transcript", "")).strip()
        ]
        raw_text = "\n".join(
            f"{turn['speaker']}: {turn['text']}" for turn in clean_turns
        )
        if not clean_turns:
            return self.fallback.extract("")
        if not (
            self.settings.direction_agent_enabled
            and self.settings.openai_api_key
            and not self.settings.demo_mode
        ):
            latest = clean_turns[-1]
            return self.fallback.extract(latest["text"], latest["stt_confidence"])

        instructions = (
            "You are Waymark's live delivery route interpreter. Read the ordered rolling "
            "conversation between a rider and customer and reconstruct the best CURRENT "
            "route guidance. People may interrupt, use pronouns, speak Nigerian English or "
            "Pidgin, spread one direction across several turns, misunderstand each other, "
            "or correct themselves. Resolve references from context. A later explicit "
            "correction replaces the conflicting earlier instruction. Return the cumulative "
            "route that is still valid, in travel order, rather than only the newest sentence. "
            "Do not include canceled or negated actions as route steps, and do not repeat the "
            "same movement in different words. "
            "Use only landmarks and directions supported by the transcript. Never invent a "
            "business, landmark, road, coordinate, distance, or turn. Put the exact supporting "
            "words from the transcript in evidence_quote. If the conversation is ambiguous, "
            "keep only unambiguous steps and set needs_clarification true. Small talk and audio "
            "checks are conversation_only. Keep instructions brief and useful to the rider."
        )
        body = {
            "model": self.settings.direction_agent_model,
            "instructions": instructions,
            "input": json.dumps(
                {"coarse_destination": coarse_address, "conversation": clean_turns},
                ensure_ascii=False,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "live_route_interpretation",
                    "strict": True,
                    "schema": ROUTE_INTERPRETATION_SCHEMA,
                },
            },
            "max_output_tokens": 700,
            "store": False,
        }
        if self.settings.direction_agent_model.startswith("gpt-5"):
            body["reasoning"] = {"effort": "minimal"}
            body["text"]["verbosity"] = "low"
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post(
                    f"{self.settings.openai_base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            response.raise_for_status()
            return self._validated(response.json(), clean_turns, raw_text)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            latest = clean_turns[-1]
            return self.fallback.extract(latest["text"], latest["stt_confidence"])

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str):
            return direct
        parts: list[str] = []
        for item in payload.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    parts.append(content["text"])
        return " ".join(parts)

    @staticmethod
    def _supported_quote(quote: Any, transcript: str) -> bool:
        normalized = normalize_name(str(quote or ""))
        return len(normalized) >= 2 and normalized in transcript

    @staticmethod
    def _supported_name(name: str, transcript: str) -> bool:
        words = [word for word in normalize_name(name).split() if len(word) > 1]
        return bool(words) and all(word in transcript.split() for word in words)

    def _validated(
        self,
        payload: dict[str, Any],
        turns: list[dict[str, Any]],
        raw_text: str,
    ) -> ExtractedDirection:
        data = json.loads(self._output_text(payload))
        transcript = normalize_name(" ".join(turn["text"] for turn in turns))
        average_stt = sum(turn["stt_confidence"] for turn in turns) / len(turns)
        landmarks: list[LandmarkPhrase] = []
        landmark_names: dict[str, str] = {}
        for item in data.get("landmarks", []):
            name = re.sub(r"\s+", " ", str(item.get("name", ""))).strip()
            normalized = normalize_name(name)
            if not normalized or not self._supported_quote(
                item.get("evidence_quote"), transcript
            ):
                continue
            if not self._supported_name(name, transcript) or normalized in landmark_names:
                continue
            confidence = max(
                0.05,
                min(0.99, float(item.get("confidence", 0.5)), average_stt),
            )
            landmark_names[normalized] = normalized
            landmarks.append(
                LandmarkPhrase(
                    name=name,
                    normalized_name=normalized,
                    landmark_type=str(item.get("landmark_type") or _landmark_type(normalized)),
                    confidence=confidence,
                )
            )

        relations: list[SpatialRelation] = []
        steps: list[RouteStep] = []
        for item in data.get("route_steps", []):
            if not self._supported_quote(item.get("evidence_quote"), transcript):
                continue
            try:
                relation_type = RelationType(str(item.get("relation_type")))
            except ValueError:
                continue
            landmark = normalize_name(str(item.get("landmark_name") or "")) or None
            reference = (
                normalize_name(str(item.get("reference_landmark_name") or "")) or None
            )
            if landmark and landmark not in landmark_names:
                continue
            if reference and reference not in landmark_names:
                continue
            confidence = max(
                0.05,
                min(0.99, float(item.get("confidence", 0.5)), average_stt),
            )
            instruction = re.sub(
                r"\s+", " ", str(item.get("instruction", ""))
            ).strip()
            if not instruction:
                continue
            distance = item.get("distance_meters")
            ordinal = item.get("ordinal")
            steps.append(
                RouteStep(
                    sequence=len(steps) + 1,
                    instruction=instruction,
                    relation_type=relation_type,
                    landmark_name=landmark,
                    confidence=confidence,
                )
            )
            relations.append(
                SpatialRelation(
                    relation_type=relation_type,
                    subject=landmark,
                    reference=reference or landmark,
                    distance_meters=(int(distance) if distance is not None else None),
                    ordinal=(int(ordinal) if ordinal is not None else None),
                    confidence=confidence,
                )
            )

        confidence = max(
            0.05,
            min(0.99, float(data.get("confidence", 0.15)), average_stt),
        )
        if data.get("needs_clarification"):
            confidence = min(confidence, 0.64)
        if not landmarks and not steps:
            confidence = min(confidence, 0.2)
        return ExtractedDirection(
            raw_text=raw_text,
            landmarks=landmarks,
            relations=relations,
            route_steps=steps,
            confidence=confidence,
        )
