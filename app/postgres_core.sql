CREATE TABLE IF NOT EXISTS delivery_sessions (
    id TEXT PRIMARY KEY,
    external_order_id TEXT NOT NULL UNIQUE,
    rider_ref TEXT NOT NULL,
    rider_phone TEXT NOT NULL,
    customer_ref TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    coarse_lat DOUBLE PRECISION NOT NULL,
    coarse_lng DOUBLE PRECISION NOT NULL,
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
    transcript_latency_ms DOUBLE PRECISION
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
    stt_confidence DOUBLE PRECISION NOT NULL,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS landmarks (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    landmark_type TEXT NOT NULL,
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    external_place_id TEXT,
    status TEXT NOT NULL DEFAULT 'observed',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
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
    confidence DOUBLE PRECISION NOT NULL,
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
    confidence DOUBLE PRECISION NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    last_observed_at TEXT NOT NULL,
    UNIQUE(from_landmark_id, to_landmark_id, relation_type)
);

CREATE TABLE IF NOT EXISTS address_nodes (
    destination_key TEXT PRIMARY KEY,
    final_lat DOUBLE PRECISION NOT NULL,
    final_lng DOUBLE PRECISION NOT NULL,
    formal_address TEXT,
    postcode TEXT,
    confidence DOUBLE PRECISION NOT NULL,
    successful_deliveries INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    delivery_id TEXT PRIMARY KEY REFERENCES delivery_sessions(id),
    trail_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_outcomes (
    delivery_id TEXT PRIMARY KEY REFERENCES delivery_sessions(id),
    delivered INTEGER NOT NULL,
    final_lat DOUBLE PRECISION NOT NULL,
    final_lng DOUBLE PRECISION NOT NULL,
    rider_confirmation INTEGER NOT NULL,
    duration_seconds INTEGER,
    retry_count INTEGER NOT NULL,
    destination_error_meters DOUBLE PRECISION,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learned_routes (
    destination_key TEXT PRIMARY KEY,
    trail_json TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    observation_count INTEGER NOT NULL,
    final_lat DOUBLE PRECISION NOT NULL,
    final_lng DOUBLE PRECISION NOT NULL,
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
