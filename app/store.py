from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .domain import (
    CallStatus,
    Coordinate,
    DeliveryCreate,
    DeliveryOutcome,
    DeliverySession,
    DeliveryStatus,
    ExtractedDirection,
    GroundedCandidate,
    Guidance,
    RouteStep,
    WaymarkEvent,
    utc_now,
)


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


def _postgres_sql(sql: str) -> str:
    statement = sql.replace("?", "%s")
    statement = statement.replace("MIN(0.99,", "LEAST(0.99,")
    if statement.lstrip().upper().startswith("INSERT OR IGNORE"):
        statement = statement.replace("INSERT OR IGNORE", "INSERT", 1)
        statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return statement


class _PostgresConnection:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.raw = connection

    def execute(self, sql: str, params: tuple | list = ()):
        return self.raw.execute(_postgres_sql(sql), params)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class SQLiteStore:
    """Small durable MVP store with explicit graph/observation separation.

    SQLite keeps the zero-key demo runnable. ``docs/postgis.sql`` is the production
    equivalent for PostgreSQL/PostGIS; this class deliberately exposes a narrow
    repository API so swapping storage does not affect the API or pipeline.
    """

    def __init__(
        self,
        path: Path | str,
        guidance_threshold: float = 0.68,
        freshness_half_life_days: int = 180,
    ) -> None:
        self.path = Path(path)
        self.guidance_threshold = guidance_threshold
        self.freshness_half_life_days = freshness_half_life_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._create_schema()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ping(self) -> bool:
        with self._lock:
            return self._connection.execute("SELECT 1").fetchone() is not None

    def _create_schema(self) -> None:
        with self._tx() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS delivery_sessions (
                    id TEXT PRIMARY KEY,
                    external_order_id TEXT NOT NULL UNIQUE,
                    rider_ref TEXT NOT NULL,
                    rider_phone TEXT NOT NULL,
                    customer_ref TEXT NOT NULL,
                    customer_phone TEXT NOT NULL,
                    coarse_lat REAL NOT NULL,
                    coarse_lng REAL NOT NULL,
                    coarse_address TEXT,
                    destination_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deliveries_destination
                    ON delivery_sessions(destination_key);

                CREATE TABLE IF NOT EXISTS proxy_mappings (
                    proxy_number TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL REFERENCES delivery_sessions(id),
                    expires_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS call_sessions (
                    id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL REFERENCES delivery_sessions(id),
                    provider TEXT NOT NULL,
                    provider_call_id TEXT,
                    proxy_number TEXT NOT NULL,
                    status TEXT NOT NULL,
                    consent_state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    transcript_latency_ms REAL
                );

                CREATE TABLE IF NOT EXISTS provider_call_legs (
                    provider_call_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL REFERENCES call_sessions(id),
                    speaker TEXT NOT NULL,
                    stream_started INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS provider_dialogs (
                    provider_dialog_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL REFERENCES call_sessions(id),
                    disclosure_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS utterances (
                    id TEXT PRIMARY KEY,
                    call_id TEXT,
                    delivery_id TEXT NOT NULL REFERENCES delivery_sessions(id),
                    speaker TEXT NOT NULL,
                    transcript TEXT NOT NULL,
                    language_mix_json TEXT NOT NULL,
                    stt_confidence REAL NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS landmarks (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    landmark_type TEXT NOT NULL,
                    lat REAL,
                    lng REAL,
                    external_place_id TEXT,
                    status TEXT NOT NULL DEFAULT 'observed',
                    confidence REAL NOT NULL DEFAULT 0,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    last_observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS landmark_aliases (
                    alias TEXT PRIMARY KEY,
                    landmark_id TEXT NOT NULL REFERENCES landmarks(id),
                    normalized_alias TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS landmark_observations (
                    id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL REFERENCES delivery_sessions(id),
                    call_id TEXT,
                    landmark_id TEXT REFERENCES landmarks(id),
                    raw_phrase TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    speaker TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS landmark_edges (
                    id TEXT PRIMARY KEY,
                    from_landmark_id TEXT REFERENCES landmarks(id),
                    to_landmark_id TEXT REFERENCES landmarks(id),
                    relation_type TEXT NOT NULL,
                    distance_hint_meters INTEGER,
                    confidence REAL NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 1,
                    last_observed_at TEXT NOT NULL,
                    UNIQUE(from_landmark_id, to_landmark_id, relation_type)
                );

                CREATE TABLE IF NOT EXISTS address_nodes (
                    destination_key TEXT PRIMARY KEY,
                    final_lat REAL NOT NULL,
                    final_lng REAL NOT NULL,
                    formal_address TEXT,
                    postcode TEXT,
                    confidence REAL NOT NULL,
                    successful_deliveries INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resolutions (
                    delivery_id TEXT PRIMARY KEY REFERENCES delivery_sessions(id),
                    trail_json TEXT NOT NULL,
                    candidates_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delivery_outcomes (
                    delivery_id TEXT PRIMARY KEY REFERENCES delivery_sessions(id),
                    delivered INTEGER NOT NULL,
                    final_lat REAL NOT NULL,
                    final_lng REAL NOT NULL,
                    rider_confirmation INTEGER NOT NULL,
                    duration_seconds INTEGER,
                    retry_count INTEGER NOT NULL,
                    destination_error_meters REAL,
                    completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS learned_routes (
                    destination_key TEXT PRIMARY KEY,
                    trail_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    observation_count INTEGER NOT NULL,
                    final_lat REAL NOT NULL,
                    final_lng REAL NOT NULL,
                    learned_from_delivery_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS route_evidence (
                    destination_key TEXT NOT NULL,
                    rider_ref TEXT NOT NULL,
                    delivery_id TEXT NOT NULL REFERENCES delivery_sessions(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(destination_key, rider_ref)
                );

                CREATE TABLE IF NOT EXISTS event_log (
                    id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL,
                    call_id TEXT,
                    type TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_delivery
                    ON event_log(delivery_id, timestamp);
                """
            )

    def create_delivery(self, payload: DeliveryCreate) -> DeliverySession:
        delivery_id = _id("del")
        now = _iso()
        destination_key = payload.destination_key or payload.customer_ref
        try:
            with self._tx() as db:
                db.execute(
                    """
                    INSERT INTO delivery_sessions (
                        id, external_order_id, rider_ref, rider_phone, customer_ref,
                        customer_phone, coarse_lat, coarse_lng, coarse_address,
                        destination_key, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delivery_id,
                        payload.external_order_id,
                        payload.rider_ref,
                        payload.rider_phone,
                        payload.customer_ref,
                        payload.customer_phone,
                        payload.coarse_location.lat,
                        payload.coarse_location.lng,
                        payload.coarse_address,
                        destination_key,
                        DeliveryStatus.CREATED,
                        now,
                        now,
                    ),
                )
        except (sqlite3.IntegrityError, psycopg.IntegrityError) as exc:
            raise ConflictError(f"order {payload.external_order_id!r} already exists") from exc
        return self.get_delivery(delivery_id)

    def _delivery_from_row(self, row: sqlite3.Row) -> DeliverySession:
        route = self._connection.execute(
            "SELECT 1 FROM learned_routes WHERE destination_key = ?", (row["destination_key"],)
        ).fetchone()
        proxy = self._connection.execute(
            "SELECT proxy_number FROM proxy_mappings WHERE delivery_id = ? AND active = 1",
            (row["id"],),
        ).fetchone()
        return DeliverySession(
            id=row["id"],
            external_order_id=row["external_order_id"],
            rider_ref=row["rider_ref"],
            customer_ref=row["customer_ref"],
            coarse_location=Coordinate(lat=row["coarse_lat"], lng=row["coarse_lng"]),
            coarse_address=row["coarse_address"],
            destination_key=row["destination_key"],
            status=row["status"],
            proxy_number=proxy["proxy_number"] if proxy else None,
            known_route_available=bool(route),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def get_delivery(self, delivery_id: str) -> DeliverySession:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM delivery_sessions WHERE id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"delivery {delivery_id!r} was not found")
            return self._delivery_from_row(row)

    def list_deliveries(self, limit: int = 50) -> list[DeliverySession]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM delivery_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._delivery_from_row(row) for row in rows]

    def get_private_delivery(self, delivery_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM delivery_sessions WHERE id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"delivery {delivery_id!r} was not found")
            return dict(row)

    def set_delivery_status(self, delivery_id: str, status: DeliveryStatus) -> None:
        with self._tx() as db:
            result = db.execute(
                "UPDATE delivery_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, _iso(), delivery_id),
            )
            if result.rowcount == 0:
                raise NotFoundError(f"delivery {delivery_id!r} was not found")

    def assign_proxy(
        self, delivery_id: str, proxy_numbers: tuple[str, ...], ttl_minutes: int = 60
    ) -> tuple[str, datetime]:
        self.get_delivery(delivery_id)
        now = utc_now()
        expires_at = now + timedelta(minutes=ttl_minutes)
        with self._tx() as db:
            db.execute(
                "UPDATE proxy_mappings SET active = 0 WHERE expires_at <= ?", (_iso(now),)
            )
            existing = db.execute(
                "SELECT proxy_number, expires_at FROM proxy_mappings "
                "WHERE delivery_id = ? AND active = 1",
                (delivery_id,),
            ).fetchone()
            if existing:
                return existing["proxy_number"], _dt(existing["expires_at"])
            for number in proxy_numbers:
                occupied = db.execute(
                    "SELECT 1 FROM proxy_mappings WHERE proxy_number = ? AND active = 1",
                    (number,),
                ).fetchone()
                if occupied:
                    continue
                db.execute(
                    """
                    INSERT INTO proxy_mappings(proxy_number, delivery_id, expires_at, active)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(proxy_number) DO UPDATE SET
                        delivery_id = excluded.delivery_id,
                        expires_at = excluded.expires_at,
                        active = 1
                    """,
                    (number, delivery_id, _iso(expires_at)),
                )
                db.execute(
                    "UPDATE delivery_sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (DeliveryStatus.PROXY_ASSIGNED, _iso(now), delivery_id),
                )
                return number, expires_at
        raise ConflictError("no proxy number is currently available")

    def delivery_for_proxy(self, proxy_number: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT d.* FROM delivery_sessions d
                JOIN proxy_mappings p ON p.delivery_id = d.id
                WHERE p.proxy_number = ? AND p.active = 1 AND p.expires_at > ?
                """,
                (proxy_number, _iso()),
            ).fetchone()
            if not row:
                raise NotFoundError(f"no active delivery uses proxy {proxy_number!r}")
            return dict(row)

    def create_call(
        self, delivery_id: str, provider: str, provider_call_id: str, proxy_number: str
    ) -> str:
        call_id = _id("call")
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO call_sessions (
                    id, delivery_id, provider, provider_call_id, proxy_number,
                    status, consent_state, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    delivery_id,
                    provider,
                    provider_call_id,
                    proxy_number,
                    CallStatus.RINGING,
                    "disclosure_pending",
                    _iso(),
                ),
            )
            db.execute(
                """
                INSERT INTO provider_call_legs(provider_call_id, call_id, speaker)
                VALUES (?, ?, 'rider')
                """,
                (provider_call_id, call_id),
            )
            db.execute(
                "UPDATE delivery_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (DeliveryStatus.CALLING, _iso(), delivery_id),
            )
        return call_id

    def get_call(self, call_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM call_sessions WHERE id = ?", (call_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"call {call_id!r} was not found")
            return dict(row)

    def call_for_provider_id(self, provider_call_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT c.* FROM call_sessions c
                JOIN provider_call_legs l ON l.call_id = c.id
                WHERE l.provider_call_id = ?
                """,
                (provider_call_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(f"provider call {provider_call_id!r} was not found")
            return dict(row)

    def add_provider_call_leg(
        self, call_id: str, provider_call_id: str, speaker: str
    ) -> None:
        if speaker not in {"rider", "customer"}:
            raise ValueError("speaker must be rider or customer")
        self.get_call(call_id)
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO provider_call_legs(provider_call_id, call_id, speaker)
                VALUES (?, ?, ?)
                ON CONFLICT(provider_call_id) DO UPDATE SET
                    call_id = excluded.call_id,
                    speaker = excluded.speaker
                """,
                (provider_call_id, call_id, speaker),
            )

    def has_provider_call_leg(self, call_id: str, speaker: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM provider_call_legs
                WHERE call_id = ? AND speaker = ?
                """,
                (call_id, speaker),
            ).fetchone()
            return row is not None

    def provider_call_leg(self, provider_call_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT l.provider_call_id, l.speaker, l.stream_started,
                       c.id AS call_id, c.delivery_id
                FROM provider_call_legs l
                JOIN call_sessions c ON c.id = l.call_id
                WHERE l.provider_call_id = ?
                """,
                (provider_call_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(f"provider call {provider_call_id!r} was not found")
            return dict(row)

    def provider_call_legs(self, call_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT provider_call_id, speaker, stream_started
                FROM provider_call_legs WHERE call_id = ?
                """,
                (call_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def add_provider_dialog(self, call_id: str, provider_dialog_id: str) -> None:
        self.get_call(call_id)
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO provider_dialogs(provider_dialog_id, call_id)
                VALUES (?, ?)
                ON CONFLICT(provider_dialog_id) DO UPDATE SET call_id = excluded.call_id
                """,
                (provider_dialog_id, call_id),
            )

    def complete_provider_dialog(
        self, call_id: str, child_call_id: str, provider_dialog_id: str
    ) -> None:
        self.get_call(call_id)
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO provider_call_legs(provider_call_id, call_id, speaker)
                VALUES (?, ?, 'customer')
                ON CONFLICT(provider_call_id) DO UPDATE SET
                    call_id = excluded.call_id,
                    speaker = 'customer'
                """,
                (child_call_id, call_id),
            )
            db.execute(
                """
                INSERT INTO provider_dialogs(provider_dialog_id, call_id)
                VALUES (?, ?)
                ON CONFLICT(provider_dialog_id) DO UPDATE SET call_id = excluded.call_id
                """,
                (provider_dialog_id, call_id),
            )

    def call_for_provider_dialog(self, provider_dialog_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT c.*, d.disclosure_requested FROM call_sessions c
                JOIN provider_dialogs d ON d.call_id = c.id
                WHERE d.provider_dialog_id = ?
                """,
                (provider_dialog_id,),
            ).fetchone()
            if not row:
                raise NotFoundError(f"provider dialog {provider_dialog_id!r} was not found")
            return dict(row)

    def mark_dialog_disclosure_requested(self, provider_dialog_id: str) -> bool:
        with self._tx() as db:
            result = db.execute(
                """
                UPDATE provider_dialogs SET disclosure_requested = 1
                WHERE provider_dialog_id = ? AND disclosure_requested = 0
                """,
                (provider_dialog_id,),
            )
        return result.rowcount == 1

    def mark_provider_stream_started(self, provider_call_id: str) -> bool:
        with self._tx() as db:
            result = db.execute(
                """
                UPDATE provider_call_legs SET stream_started = 1
                WHERE provider_call_id = ? AND stream_started = 0
                """,
                (provider_call_id,),
            )
        return result.rowcount == 1

    def update_call(
        self,
        call_id: str,
        status: CallStatus,
        *,
        consent_state: str | None = None,
        transcript_latency_ms: float | None = None,
    ) -> dict[str, Any]:
        fields = ["status = ?"]
        values: list[Any] = [status]
        if status in {CallStatus.DISCONNECTED, CallStatus.FAILED, CallStatus.TIMEOUT}:
            fields.append("ended_at = ?")
            values.append(_iso())
        if consent_state is not None:
            fields.append("consent_state = ?")
            values.append(consent_state)
        if transcript_latency_ms is not None:
            fields.append("transcript_latency_ms = ?")
            values.append(transcript_latency_ms)
        values.append(call_id)
        with self._tx() as db:
            result = db.execute(
                f"UPDATE call_sessions SET {', '.join(fields)} WHERE id = ?", values
            )
            if result.rowcount == 0:
                raise NotFoundError(f"call {call_id!r} was not found")
        return self.get_call(call_id)

    def add_utterance(
        self,
        delivery_id: str,
        call_id: str | None,
        speaker: str,
        transcript: str,
        language_mix: list[str],
        confidence: float,
        started_at_ms: int,
        ended_at_ms: int,
    ) -> str:
        utterance_id = _id("utt")
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO utterances (
                    id, call_id, delivery_id, speaker, transcript, language_mix_json,
                    stt_confidence, started_at_ms, ended_at_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utterance_id,
                    call_id,
                    delivery_id,
                    speaker,
                    transcript,
                    json.dumps(language_mix),
                    confidence,
                    started_at_ms,
                    ended_at_ms,
                    _iso(),
                ),
            )
        return utterance_id

    def recent_utterances(
        self, delivery_id: str, *, call_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return a small chronological window for live conversation understanding."""

        self.get_delivery(delivery_id)
        where = "delivery_id = ?"
        values: list[Any] = [delivery_id]
        if call_id is not None:
            where += " AND call_id = ?"
            values.append(call_id)
        values.append(max(1, min(limit, 20)))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT speaker, transcript, stt_confidence, created_at
                FROM utterances WHERE {where}
                ORDER BY created_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [
            {
                "speaker": row["speaker"],
                "transcript": row["transcript"],
                "confidence": float(row["stt_confidence"]),
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def save_resolution(
        self,
        delivery_id: str,
        call_id: str | None,
        extraction: ExtractedDirection,
        candidates: dict[str, list[GroundedCandidate]],
        guidance: Guidance,
        speaker: str,
    ) -> None:
        now = _iso()
        landmark_ids: dict[str, str] = {}
        with self._tx() as db:
            for landmark in extraction.landmarks:
                existing = db.execute(
                    "SELECT id, observation_count, confidence FROM landmarks "
                    "WHERE normalized_name = ?",
                    (landmark.normalized_name,),
                ).fetchone()
                best = candidates.get(landmark.normalized_name, [])
                top = best[0] if best else None
                if existing:
                    landmark_id = existing["id"]
                    count = existing["observation_count"] + 1
                    confidence = (
                        existing["confidence"] * existing["observation_count"]
                        + landmark.confidence
                    ) / count
                    db.execute(
                        """
                        UPDATE landmarks SET observation_count = ?, confidence = ?,
                            last_observed_at = ?, lat = COALESCE(?, lat),
                            lng = COALESCE(?, lng),
                            external_place_id = COALESCE(?, external_place_id)
                        WHERE id = ?
                        """,
                        (
                            count,
                            confidence,
                            now,
                            top.location.lat if top else None,
                            top.location.lng if top else None,
                            top.place_id if top else None,
                            landmark_id,
                        ),
                    )
                else:
                    landmark_id = _id("lmk")
                    db.execute(
                        """
                        INSERT INTO landmarks (
                            id, canonical_name, normalized_name, landmark_type, lat, lng,
                            external_place_id, confidence, observation_count, last_observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            landmark_id,
                            landmark.name,
                            landmark.normalized_name,
                            landmark.landmark_type,
                            top.location.lat if top else None,
                            top.location.lng if top else None,
                            top.place_id if top else None,
                            landmark.confidence,
                            now,
                        ),
                    )
                landmark_ids[landmark.normalized_name] = landmark_id
                db.execute(
                    """
                    INSERT INTO landmark_observations (
                        id, delivery_id, call_id, landmark_id, raw_phrase,
                        confidence, speaker, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("obs"),
                        delivery_id,
                        call_id,
                        landmark_id,
                        landmark.name,
                        landmark.confidence,
                        speaker,
                        now,
                    ),
                )

            ordered = [
                landmark_ids[step.landmark_name]
                for step in extraction.route_steps
                if step.landmark_name in landmark_ids
            ]
            for index, relation in enumerate(extraction.relations):
                from_id = ordered[index] if index < len(ordered) else None
                to_id = ordered[index + 1] if index + 1 < len(ordered) else None
                db.execute(
                    """
                    INSERT INTO landmark_edges (
                        id, from_landmark_id, to_landmark_id, relation_type,
                        distance_hint_meters, confidence, last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(from_landmark_id, to_landmark_id, relation_type) DO UPDATE SET
                        confidence = (landmark_edges.confidence * landmark_edges.observation_count
                            + excluded.confidence) / (landmark_edges.observation_count + 1),
                        observation_count = landmark_edges.observation_count + 1,
                        last_observed_at = excluded.last_observed_at
                    """,
                    (
                        _id("edge"),
                        from_id,
                        to_id,
                        relation.relation_type,
                        relation.distance_meters,
                        relation.confidence,
                        now,
                    ),
                )

            db.execute(
                """
                INSERT INTO resolutions (
                    delivery_id, trail_json, candidates_json, confidence, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    trail_json = excluded.trail_json,
                    candidates_json = excluded.candidates_json,
                    confidence = excluded.confidence,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    delivery_id,
                    json.dumps([step.model_dump(mode="json") for step in guidance.trail]),
                    json.dumps(
                        {
                            key: [item.model_dump(mode="json") for item in value]
                            for key, value in candidates.items()
                        }
                    ),
                    guidance.confidence,
                    guidance.source,
                    now,
                ),
            )
            db.execute(
                "UPDATE delivery_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (DeliveryStatus.GUIDING, now, delivery_id),
            )

    def get_guidance(self, delivery_id: str) -> Guidance | None:
        self.get_delivery(delivery_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM resolutions WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                return None
            candidates = {
                key: [GroundedCandidate.model_validate(item) for item in value]
                for key, value in json.loads(row["candidates_json"]).items()
            }
            return Guidance(
                delivery_id=delivery_id,
                status=(
                    "resolved"
                    if row["confidence"] >= self.guidance_threshold
                    else "low_confidence"
                ),
                confidence=row["confidence"],
                trail=[RouteStep.model_validate(item) for item in json.loads(row["trail_json"])],
                candidates=candidates,
                source=row["source"],
                updated_at=_dt(row["updated_at"]),
            )

    def learned_guidance(self, delivery_id: str) -> Guidance | None:
        delivery = self.get_delivery(delivery_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM learned_routes WHERE destination_key = ?",
                (delivery.destination_key,),
            ).fetchone()
            if not row:
                return None
            confidence = self._fresh_confidence(row["confidence"], row["updated_at"])
            return Guidance(
                delivery_id=delivery_id,
                status=(
                    "resolved" if confidence >= self.guidance_threshold else "low_confidence"
                ),
                confidence=confidence,
                trail=[RouteStep.model_validate(item) for item in json.loads(row["trail_json"])],
                candidates={},
                source="human_address_graph",
                updated_at=_dt(row["updated_at"]),
            )

    def resolve_destination(self, destination_key: str) -> Guidance | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM learned_routes WHERE destination_key = ?", (destination_key,)
            ).fetchone()
            if not row:
                return None
            confidence = self._fresh_confidence(row["confidence"], row["updated_at"])
            return Guidance(
                delivery_id="unassigned",
                status=(
                    "resolved" if confidence >= self.guidance_threshold else "low_confidence"
                ),
                confidence=confidence,
                trail=[RouteStep.model_validate(item) for item in json.loads(row["trail_json"])],
                candidates={},
                source="human_address_graph",
                updated_at=_dt(row["updated_at"]),
            )

    def _fresh_confidence(self, confidence: float, updated_at: str) -> float:
        age_days = max(0.0, (utc_now() - _dt(updated_at)).total_seconds() / 86_400)
        decay = 0.5 ** (age_days / max(1, self.freshness_half_life_days))
        return round(max(0.05, confidence * decay), 4)

    def get_outcome(self, delivery_id: str) -> DeliveryOutcome | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM delivery_outcomes WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                return None
            delivery = self.get_delivery(delivery_id)
            learned = bool(
                row["delivered"] and self.resolve_destination(delivery.destination_key)
            )
            return DeliveryOutcome(
                delivery_id=delivery_id,
                delivered=bool(row["delivered"]),
                final_location=Coordinate(lat=row["final_lat"], lng=row["final_lng"]),
                destination_error_meters=row["destination_error_meters"],
                learned=learned,
                completed_at=_dt(row["completed_at"]),
            )

    def save_reused_guidance(self, guidance: Guidance) -> None:
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO resolutions (
                    delivery_id, trail_json, candidates_json, confidence, source, updated_at
                ) VALUES (?, ?, '{}', ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    trail_json = excluded.trail_json,
                    candidates_json = '{}', confidence = excluded.confidence,
                    source = excluded.source, updated_at = excluded.updated_at
                """,
                (
                    guidance.delivery_id,
                    json.dumps([step.model_dump(mode="json") for step in guidance.trail]),
                    guidance.confidence,
                    guidance.source,
                    _iso(guidance.updated_at),
                ),
            )
            db.execute(
                "UPDATE delivery_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (
                    DeliveryStatus.RESOLVED
                    if guidance.status == "resolved"
                    else DeliveryStatus.GUIDING,
                    _iso(),
                    guidance.delivery_id,
                ),
            )

    def complete_delivery(
        self,
        delivery_id: str,
        delivered: bool,
        final_location: Coordinate,
        rider_confirmation: bool,
        duration_seconds: int | None,
        retry_count: int,
        destination_error_meters: float | None,
    ) -> bool:
        delivery = self.get_delivery(delivery_id)
        guidance = self.get_guidance(delivery_id)
        learned = bool(delivered and rider_confirmation and guidance and guidance.trail)
        now = _iso()
        with self._tx() as db:
            db.execute(
                """
                INSERT INTO delivery_outcomes (
                    delivery_id, delivered, final_lat, final_lng, rider_confirmation,
                    duration_seconds, retry_count, destination_error_meters, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO UPDATE SET
                    delivered = excluded.delivered, final_lat = excluded.final_lat,
                    final_lng = excluded.final_lng,
                    rider_confirmation = excluded.rider_confirmation,
                    duration_seconds = excluded.duration_seconds,
                    retry_count = excluded.retry_count,
                    destination_error_meters = excluded.destination_error_meters,
                    completed_at = excluded.completed_at
                """,
                (
                    delivery_id,
                    int(delivered),
                    final_location.lat,
                    final_location.lng,
                    int(rider_confirmation),
                    duration_seconds,
                    retry_count,
                    destination_error_meters,
                    now,
                ),
            )
            db.execute(
                "UPDATE delivery_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (
                    DeliveryStatus.DELIVERED if delivered else DeliveryStatus.FAILED,
                    now,
                    delivery_id,
                ),
            )
            db.execute(
                "UPDATE proxy_mappings SET active = 0 WHERE delivery_id = ?", (delivery_id,)
            )
            if learned and guidance:
                existing = db.execute(
                    """
                    SELECT confidence, observation_count FROM learned_routes
                    WHERE destination_key = ?
                    """,
                    (delivery.destination_key,),
                ).fetchone()
                evidence = db.execute(
                    """
                    SELECT 1 FROM route_evidence
                    WHERE destination_key = ? AND rider_ref = ?
                    """,
                    (delivery.destination_key, delivery.rider_ref),
                ).fetchone()
                independent = evidence is None
                if independent:
                    db.execute(
                        """
                        INSERT INTO route_evidence (
                            destination_key, rider_ref, delivery_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (delivery.destination_key, delivery.rider_ref, delivery_id, now),
                    )
                if existing:
                    if independent:
                        count = existing["observation_count"] + 1
                        confidence = min(
                            0.98,
                            (
                                existing["confidence"] * existing["observation_count"]
                                + guidance.confidence
                            )
                            / count
                            + 0.03,
                        )
                    else:
                        count = existing["observation_count"]
                        confidence = existing["confidence"]
                else:
                    count = 1
                    confidence = min(0.95, guidance.confidence + 0.05)
                db.execute(
                    """
                    INSERT INTO learned_routes (
                        destination_key, trail_json, confidence, observation_count,
                        final_lat, final_lng, learned_from_delivery_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(destination_key) DO UPDATE SET
                        trail_json = excluded.trail_json,
                        confidence = excluded.confidence,
                        observation_count = excluded.observation_count,
                        final_lat = excluded.final_lat, final_lng = excluded.final_lng,
                        learned_from_delivery_id = excluded.learned_from_delivery_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        delivery.destination_key,
                        json.dumps([step.model_dump(mode="json") for step in guidance.trail]),
                        confidence,
                        count,
                        final_location.lat,
                        final_location.lng,
                        delivery_id,
                        now,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO address_nodes (
                        destination_key, final_lat, final_lng, confidence,
                        successful_deliveries, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(destination_key) DO UPDATE SET
                        final_lat = excluded.final_lat, final_lng = excluded.final_lng,
                        confidence = CASE
                            WHEN excluded.successful_deliveries = 1
                            THEN MIN(0.99, address_nodes.confidence + 0.03)
                            ELSE address_nodes.confidence
                        END,
                        successful_deliveries = address_nodes.successful_deliveries
                            + excluded.successful_deliveries,
                        updated_at = excluded.updated_at
                    """,
                    (
                        delivery.destination_key,
                        final_location.lat,
                        final_location.lng,
                        confidence,
                        int(independent),
                        now,
                    ),
                )
                db.execute(
                    """
                    UPDATE landmark_observations SET verified = 1
                    WHERE delivery_id = ?
                    """,
                    (delivery_id,),
                )
                db.execute(
                    """
                    UPDATE landmarks SET status = 'corroborated',
                        confidence = MIN(0.99, confidence + 0.05)
                    WHERE id IN (
                        SELECT landmark_id FROM landmark_observations WHERE delivery_id = ?
                    )
                    """,
                    (delivery_id,),
                )
        return learned

    def save_event(self, event: WaymarkEvent) -> None:
        with self._tx() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO event_log (
                    id, delivery_id, call_id, type, trace_id, data_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.delivery_id,
                    event.call_id,
                    event.type,
                    event.trace_id,
                    json.dumps(event.data),
                    _iso(event.timestamp),
                ),
            )

    def event_history(self, delivery_id: str, limit: int = 100) -> list[WaymarkEvent]:
        self.get_delivery(delivery_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM event_log WHERE delivery_id = ?
                ORDER BY timestamp ASC LIMIT ?
                """,
                (delivery_id, limit),
            ).fetchall()
            return [
                WaymarkEvent(
                    id=row["id"],
                    type=row["type"],
                    delivery_id=row["delivery_id"],
                    call_id=row["call_id"],
                    trace_id=row["trace_id"],
                    timestamp=_dt(row["timestamp"]),
                    data=json.loads(row["data_json"]),
                )
                for row in rows
            ]


class PostgresStore(SQLiteStore):
    """PostgreSQL-backed store using the same repository contract as local SQLite."""

    def __init__(
        self,
        database_url: str,
        guidance_threshold: float = 0.68,
        freshness_half_life_days: int = 180,
    ) -> None:
        self.path = Path("postgresql")
        self.guidance_threshold = guidance_threshold
        self.freshness_half_life_days = freshness_half_life_days
        self._lock = threading.RLock()
        raw = psycopg.connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=10,
        )
        self._connection = _PostgresConnection(raw)
        self._create_schema()

    def _create_schema(self) -> None:
        schema_path = Path(__file__).with_name("postgres_core.sql")
        statements = [
            statement.strip()
            for statement in schema_path.read_text(encoding="utf-8").split(";")
            if statement.strip()
        ]
        with self._tx() as db:
            for statement in statements:
                db.execute(statement)
