from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryStatus(StrEnum):
    CREATED = "created"
    PROXY_ASSIGNED = "proxy_assigned"
    CALLING = "calling"
    GUIDING = "guiding"
    RESOLVED = "resolved"
    DELIVERED = "delivered"
    FAILED = "failed"


class CallStatus(StrEnum):
    RINGING = "ringing"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RelationType(StrEnum):
    PASS = "pass"
    AFTER = "after"
    BEFORE = "before"
    OPPOSITE = "opposite"
    BESIDE = "beside"
    LEFT_OF = "left_of"
    RIGHT_OF = "right_of"
    NEAR = "near"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    CONTINUE = "continue"


class EventType(StrEnum):
    CALL_STATUS = "call.status"
    TRANSCRIPT_PARTIAL = "transcript.partial"
    TRANSCRIPT_FINAL = "transcript.final"
    LANDMARK_DETECTED = "landmark.detected"
    GUIDANCE_UPDATED = "guidance.updated"
    GUIDANCE_UNCERTAIN = "guidance.uncertain"
    DELIVERY_COMPLETED = "delivery.completed"
    ROUTE_LEARNED = "route.learned"
    ROUTE_REUSED = "route.reused"
    ERROR = "error"


class Coordinate(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class DeliveryCreate(BaseModel):
    external_order_id: str = Field(min_length=1, max_length=128)
    rider_ref: str = Field(min_length=1, max_length=128)
    rider_phone: str = Field(min_length=7, max_length=32)
    customer_ref: str = Field(min_length=1, max_length=128)
    customer_phone: str = Field(min_length=7, max_length=32)
    coarse_location: Coordinate
    coarse_address: str | None = Field(default=None, max_length=500)
    destination_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("rider_phone", "customer_phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        value = value.strip().replace(" ", "").replace("-", "")
        if not value.startswith("+") or not value[1:].isdigit():
            raise ValueError("phone numbers must use E.164 format, for example +2348012345678")
        return value


class DeliverySession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    external_order_id: str
    rider_ref: str
    customer_ref: str
    coarse_location: Coordinate
    coarse_address: str | None
    destination_key: str
    status: DeliveryStatus
    proxy_number: str | None = None
    known_route_available: bool = False
    created_at: datetime
    updated_at: datetime


class ProxyAssignment(BaseModel):
    delivery_id: str
    proxy_number: str
    expires_at: datetime


class WebRTCCallLinks(BaseModel):
    delivery_id: str
    call_id: str
    provider: str = "daily"
    rider_url: str
    customer_url: str
    expires_at: datetime


class WebRTCJoin(BaseModel):
    room_url: str
    meeting_token: str
    audio_websocket_url: str
    role: str
    disclosure: str
    expires_at: datetime


class Utterance(BaseModel):
    id: str
    call_id: str | None
    delivery_id: str
    speaker: str
    transcript: str
    language_mix: list[str]
    stt_confidence: float = Field(ge=0, le=1)
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int = Field(ge=0)
    created_at: datetime


class LandmarkPhrase(BaseModel):
    name: str
    normalized_name: str
    landmark_type: str
    confidence: float = Field(ge=0, le=1)


class SpatialRelation(BaseModel):
    relation_type: RelationType
    subject: str | None = None
    reference: str | None = None
    distance_meters: int | None = Field(default=None, ge=0)
    ordinal: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0, le=1)


class RouteStep(BaseModel):
    sequence: int = Field(ge=1)
    instruction: str
    relation_type: RelationType
    landmark_name: str | None = None
    confidence: float = Field(ge=0, le=1)


class ExtractedDirection(BaseModel):
    raw_text: str
    landmarks: list[LandmarkPhrase]
    relations: list[SpatialRelation]
    route_steps: list[RouteStep]
    confidence: float = Field(ge=0, le=1)


class GroundedCandidate(BaseModel):
    phrase: str
    place_id: str | None
    name: str
    formatted_address: str | None
    location: Coordinate
    distance_meters: float = Field(ge=0)
    name_score: float = Field(ge=0, le=1)
    distance_score: float = Field(ge=0, le=1)
    prior_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    source: str


class Guidance(BaseModel):
    delivery_id: str
    status: str
    confidence: float = Field(ge=0, le=1)
    trail: list[RouteStep]
    candidates: dict[str, list[GroundedCandidate]] = Field(default_factory=dict)
    source: str
    updated_at: datetime


class DeliveryComplete(BaseModel):
    delivered: bool
    final_location: Coordinate
    rider_confirmation: bool = True
    duration_seconds: int | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)


class DeliveryOutcome(BaseModel):
    delivery_id: str
    delivered: bool
    final_location: Coordinate
    destination_error_meters: float | None
    learned: bool
    completed_at: datetime


class SimulationUtterance(BaseModel):
    transcript: str = Field(min_length=1, max_length=4000)
    speaker: str = Field(default="customer", pattern="^(customer|rider)$")
    language_mix: list[str] = Field(default_factory=lambda: ["pcm", "en"])
    confidence: float = Field(default=0.92, ge=0, le=1)
    timestamp_ms: int = Field(default=0, ge=0)


class ResolveResponse(BaseModel):
    destination_key: str
    found: bool
    guidance: Guidance | None


class WaymarkEvent(BaseModel):
    id: str
    type: EventType
    delivery_id: str
    call_id: str | None = None
    trace_id: str
    timestamp: datetime
    data: dict[str, Any]


class DemoRunRequest(BaseModel):
    transcript: str = (
        "Pass the Mobil filling station, take the second right, "
        "then look for the black gate opposite the mosque."
    )
    final_location: Coordinate = Coordinate(lat=6.5162, lng=3.3862)


class DemoRunResponse(BaseModel):
    first_delivery: DeliverySession
    learned_guidance: Guidance
    second_delivery: DeliverySession
    reused_guidance: Guidance
