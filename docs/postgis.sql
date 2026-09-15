-- Waymark production schema for PostgreSQL 16 + PostGIS 3.
-- SQLite is used by the zero-dependency demo; this schema is the pilot deployment target.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE delivery_sessions (
    id text PRIMARY KEY,
    external_order_id text NOT NULL UNIQUE,
    rider_ref text NOT NULL,
    rider_phone_ciphertext bytea NOT NULL,
    customer_ref text NOT NULL,
    customer_phone_ciphertext bytea NOT NULL,
    coarse_location geography(Point, 4326) NOT NULL,
    coarse_address text,
    destination_key text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX delivery_sessions_destination_idx ON delivery_sessions(destination_key);
CREATE INDEX delivery_sessions_location_gix ON delivery_sessions USING gist(coarse_location);

CREATE TABLE proxy_mappings (
    proxy_number text PRIMARY KEY,
    delivery_id text NOT NULL REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    active boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX one_active_proxy_per_delivery_idx
    ON proxy_mappings(delivery_id) WHERE active;

CREATE TABLE call_sessions (
    id text PRIMARY KEY,
    delivery_id text NOT NULL REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    provider text NOT NULL,
    provider_call_id text UNIQUE,
    proxy_number text NOT NULL,
    status text NOT NULL,
    consent_state text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    transcript_latency_ms double precision
);

CREATE TABLE utterances (
    id text PRIMARY KEY,
    call_id text REFERENCES call_sessions(id) ON DELETE SET NULL,
    delivery_id text NOT NULL REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    speaker text NOT NULL,
    transcript text NOT NULL,
    language_mix text[] NOT NULL DEFAULT '{}',
    stt_confidence double precision NOT NULL CHECK (stt_confidence BETWEEN 0 AND 1),
    started_at_ms integer NOT NULL,
    ended_at_ms integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE landmarks (
    id text PRIMARY KEY,
    canonical_name text NOT NULL,
    normalized_name text NOT NULL UNIQUE,
    landmark_type text NOT NULL,
    location geography(Point, 4326),
    external_place_id text,
    status text NOT NULL DEFAULT 'observed',
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    observation_count integer NOT NULL DEFAULT 0,
    last_observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX landmarks_location_gix ON landmarks USING gist(location);
CREATE INDEX landmarks_name_trgm_idx ON landmarks USING gin(normalized_name gin_trgm_ops);

CREATE TABLE landmark_aliases (
    alias text PRIMARY KEY,
    normalized_alias text NOT NULL,
    landmark_id text NOT NULL REFERENCES landmarks(id) ON DELETE CASCADE
);
CREATE INDEX landmark_aliases_trgm_idx ON landmark_aliases USING gin(normalized_alias gin_trgm_ops);

CREATE TABLE landmark_observations (
    id text PRIMARY KEY,
    delivery_id text NOT NULL REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    landmark_id text REFERENCES landmarks(id),
    raw_phrase text NOT NULL,
    speaker text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    verified boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE landmark_edges (
    id text PRIMARY KEY,
    from_landmark_id text REFERENCES landmarks(id),
    to_landmark_id text REFERENCES landmarks(id),
    relation_type text NOT NULL,
    distance_hint_meters integer,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    observation_count integer NOT NULL DEFAULT 1,
    last_observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (from_landmark_id, to_landmark_id, relation_type)
);

CREATE TABLE address_nodes (
    destination_key text PRIMARY KEY,
    final_location geography(Point, 4326) NOT NULL,
    formal_address text,
    postcode text,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    successful_deliveries integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX address_nodes_location_gix ON address_nodes USING gist(final_location);

CREATE TABLE resolutions (
    delivery_id text PRIMARY KEY REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    candidate_path jsonb NOT NULL DEFAULT '[]',
    final_path jsonb NOT NULL DEFAULT '[]',
    candidates jsonb NOT NULL DEFAULT '{}',
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE delivery_outcomes (
    delivery_id text PRIMARY KEY REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    delivered boolean NOT NULL,
    final_location geography(Point, 4326) NOT NULL,
    rider_confirmation boolean NOT NULL,
    duration_seconds integer,
    retry_count integer NOT NULL DEFAULT 0,
    destination_error_meters double precision,
    completed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX delivery_outcomes_location_gix ON delivery_outcomes USING gist(final_location);

CREATE TABLE learned_routes (
    destination_key text PRIMARY KEY REFERENCES address_nodes(destination_key) ON DELETE CASCADE,
    trail jsonb NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    observation_count integer NOT NULL DEFAULT 1,
    final_location geography(Point, 4326) NOT NULL,
    learned_from_delivery_id text NOT NULL REFERENCES delivery_sessions(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE route_evidence (
    destination_key text NOT NULL REFERENCES learned_routes(destination_key) ON DELETE CASCADE,
    rider_ref text NOT NULL,
    delivery_id text NOT NULL REFERENCES delivery_sessions(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(destination_key, rider_ref)
);

CREATE TABLE event_log (
    id text PRIMARY KEY,
    delivery_id text NOT NULL REFERENCES delivery_sessions(id) ON DELETE CASCADE,
    call_id text REFERENCES call_sessions(id) ON DELETE SET NULL,
    type text NOT NULL,
    trace_id text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX event_log_delivery_idx ON event_log(delivery_id, created_at);

-- Candidate corridor query (parameters: rider point, destination point, radius in metres).
-- ST_DWithin uses the spatial index and geography returns metre distances.
-- SELECT id, canonical_name, ST_Distance(location, :rider_point) AS distance_m
-- FROM landmarks
-- WHERE ST_DWithin(location, ST_MakeLine(:rider_point::geometry,
--       :destination_point::geometry)::geography, :radius_m)
-- ORDER BY confidence DESC, distance_m ASC;
