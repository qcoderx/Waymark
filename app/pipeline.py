from __future__ import annotations

import time
import uuid

from .config import Settings
from .domain import (
    Coordinate,
    DeliveryComplete,
    DeliveryOutcome,
    EventType,
    Guidance,
    SimulationUtterance,
    utc_now,
)
from .events import EventHub
from .extraction import ConversationDirectionPlanner, DirectionExtractor
from .grounding import MapboxGrounder, haversine_meters
from .store import SQLiteStore


class ResolutionPipeline:
    def __init__(self, settings: Settings, store: SQLiteStore, events: EventHub) -> None:
        self.settings = settings
        self.store = store
        self.events = events
        self.extractor = DirectionExtractor()
        self.direction_planner = ConversationDirectionPlanner(settings, self.extractor)
        self.grounder = MapboxGrounder(settings)
        self._analysis_revisions: dict[str, int] = {}
        self._applied_revisions: dict[str, int] = {}

    async def publish_partial(
        self,
        delivery_id: str,
        transcript: str,
        *,
        call_id: str | None,
        trace_id: str,
    ) -> None:
        await self.events.publish(
            EventType.TRANSCRIPT_PARTIAL,
            delivery_id,
            {"transcript": transcript},
            call_id=call_id,
            trace_id=trace_id,
        )

    async def process_utterance(
        self,
        delivery_id: str,
        utterance: SimulationUtterance,
        *,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> Guidance:
        trace_id = trace_id or f"trace_{uuid.uuid4().hex}"
        started = time.perf_counter()
        delivery = self.store.get_delivery(delivery_id)
        self.store.add_utterance(
            delivery_id=delivery_id,
            call_id=call_id,
            speaker=utterance.speaker,
            transcript=utterance.transcript,
            language_mix=utterance.language_mix,
            confidence=utterance.confidence,
            started_at_ms=utterance.timestamp_ms,
            ended_at_ms=utterance.timestamp_ms,
        )
        await self.events.publish(
            EventType.TRANSCRIPT_FINAL,
            delivery_id,
            {
                "transcript": utterance.transcript,
                "speaker": utterance.speaker,
                "language_mix": utterance.language_mix,
                "confidence": utterance.confidence,
            },
            call_id=call_id,
            trace_id=trace_id,
        )

        revision = self._analysis_revisions.get(delivery_id, 0) + 1
        self._analysis_revisions[delivery_id] = revision
        recent_turns = self.store.recent_utterances(
            delivery_id, call_id=call_id, limit=10
        )
        extraction = await self.direction_planner.extract(
            recent_turns,
            coarse_address=delivery.coarse_address,
        )
        if revision < self._applied_revisions.get(delivery_id, 0):
            existing = self.store.get_guidance(delivery_id)
            if existing:
                return existing
            return Guidance(
                delivery_id=delivery_id,
                status="listening",
                confidence=0.15,
                trail=[],
                candidates={},
                source="live_call" if call_id else "simulated_call",
                updated_at=utc_now(),
            )
        self._applied_revisions[delivery_id] = revision
        if not extraction.landmarks and not extraction.route_steps:
            existing = self.store.get_guidance(delivery_id)
            if existing:
                return existing
            guidance = Guidance(
                delivery_id=delivery_id,
                status="listening",
                confidence=extraction.confidence,
                trail=[],
                candidates={},
                source="live_call" if call_id else "simulated_call",
                updated_at=utc_now(),
            )
            await self.events.publish(
                EventType.GUIDANCE_UNCERTAIN,
                delivery_id,
                {"guidance": guidance.model_dump(mode="json"), "reason": "no_route_evidence"},
                call_id=call_id,
                trace_id=trace_id,
            )
            return guidance
        candidates = await self.grounder.ground_all(
            extraction.landmarks, delivery.coarse_location
        )
        top_scores = [items[0].confidence for items in candidates.values() if items]
        grounding_score = sum(top_scores) / len(top_scores) if top_scores else 0.25
        confidence = round(0.68 * extraction.confidence + 0.32 * grounding_score, 4)
        status = (
            "resolved"
            if extraction.route_steps
            and confidence >= self.settings.guidance_confidence_threshold
            else "low_confidence"
        )
        guidance = Guidance(
            delivery_id=delivery_id,
            status=status,
            confidence=confidence,
            trail=extraction.route_steps,
            candidates=candidates,
            source="live_call" if call_id else "simulated_call",
            updated_at=utc_now(),
        )
        self.store.save_resolution(
            delivery_id, call_id, extraction, candidates, guidance, utterance.speaker
        )

        for landmark in extraction.landmarks:
            top = candidates.get(landmark.normalized_name, [])
            await self.events.publish(
                EventType.LANDMARK_DETECTED,
                delivery_id,
                {
                    "landmark": landmark.model_dump(mode="json"),
                    "top_candidate": top[0].model_dump(mode="json") if top else None,
                },
                call_id=call_id,
                trace_id=trace_id,
            )
        event_type = (
            EventType.GUIDANCE_UPDATED
            if status == "resolved"
            else EventType.GUIDANCE_UNCERTAIN
        )
        await self.events.publish(
            event_type,
            delivery_id,
            {
                "guidance": guidance.model_dump(mode="json"),
                "processing_latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            call_id=call_id,
            trace_id=trace_id,
        )
        if call_id:
            self.store.update_call(
                call_id,
                status="connected",
                transcript_latency_ms=(time.perf_counter() - started) * 1000,
            )
        return guidance

    async def reuse_known_route(self, delivery_id: str) -> Guidance | None:
        guidance = self.store.learned_guidance(delivery_id)
        if not guidance:
            return None
        self.store.save_reused_guidance(guidance)
        await self.events.publish(
            EventType.ROUTE_REUSED,
            delivery_id,
            {"guidance": guidance.model_dump(mode="json")},
        )
        return guidance

    async def complete(
        self, delivery_id: str, payload: DeliveryComplete
    ) -> DeliveryOutcome:
        existing = self.store.get_outcome(delivery_id)
        if existing:
            return existing
        guidance = self.store.get_guidance(delivery_id)
        resolved_location: Coordinate | None = None
        if guidance:
            for items in reversed(list(guidance.candidates.values())):
                if items:
                    resolved_location = items[0].location
                    break
        destination_error = (
            haversine_meters(resolved_location, payload.final_location)
            if resolved_location
            else None
        )
        learned = self.store.complete_delivery(
            delivery_id=delivery_id,
            delivered=payload.delivered,
            final_location=payload.final_location,
            rider_confirmation=payload.rider_confirmation,
            duration_seconds=payload.duration_seconds,
            retry_count=payload.retry_count,
            destination_error_meters=destination_error,
        )
        outcome = self.store.get_outcome(delivery_id)
        if outcome is None:
            raise RuntimeError("delivery outcome was not persisted")
        await self.events.publish(
            EventType.DELIVERY_COMPLETED,
            delivery_id,
            outcome.model_dump(mode="json"),
        )
        if learned:
            await self.events.publish(
                EventType.ROUTE_LEARNED,
                delivery_id,
                {
                    "destination_key": self.store.get_delivery(delivery_id).destination_key,
                    "confidence": guidance.confidence if guidance else 0,
                },
            )
        return outcome
