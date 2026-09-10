# Waymark

Waymark turns the phone calls riders already make for directions into reusable,
machine-navigable addresses. This repository contains the Dev 1 technical core:
proxy-call routing, live Sahara transcription, landmark extraction, map grounding,
live guidance events, delivery confirmation, and the Human Address Graph learning loop.

## What works

- Delivery sessions and temporary order-to-proxy mappings
- Infobip Calls API event handling and rider/customer Dialog bridging
- Infobip per-leg media streaming with 48 kHz PCM16 to 16 kHz Sahara conversion
- Optional Twilio webhook and Media Streams fallback
- Intron Sahara streaming STT with buffered chunks, partial transcripts, and final commits
- Conservative English/Pidgin direction extraction for landmarks, turns, ordinals, distances,
  and relative positions
- Mapbox Search Box and Geocoding v6 grounding with proximity bias and ranked candidates
- Confidence-aware rider guidance over a replayable WebSocket event stream
- Separate observations, canonical landmarks, spatial edges, outcomes, and learned routes
- Successful-delivery learning and immediate second-delivery route reuse
- A three-model benchmark harness for WER, landmark recall, relation accuracy, route success,
  and latency
- A no-key demo mode that exercises the same pipeline with deterministic map candidates

## Run the complete proof locally

Python 3.12 or later is required.

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs`, then run `POST /v1/demo/run`. The response contains
the first delivery's extracted trail and the second delivery's graph-reused trail. No
Infobip, Sahara, Mapbox, Redis, or Postgres account is required for this proof.

You can also run it from PowerShell:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://localhost:8000/v1/demo/run `
  -ContentType application/json `
  -Body '{}'
```

## Credentials to bring for a live call

Copy `.env.example` to `.env` and fill in:

1. A public HTTPS URL that forwards to this service. Waymark derives the secure media
   WebSocket URL from `PUBLIC_BASE_URL`.
2. An Infobip API key/base URL, a Calls Configuration, a `VOICE_VIDEO` event subscription,
   and at least one leased voice-capable number. Route subscription events to
   `POST {PUBLIC_BASE_URL}/v1/telephony/infobip/events`.
3. A `MEDIA_STREAMING` configuration targeting the WebSocket form of
   `{PUBLIC_BASE_URL}/v1/telephony/infobip/media`. Once `PUBLIC_BASE_URL` is public,
   `python -m app.infobip_setup` creates it and prints the ID to place in
   `INFOBIP_MEDIA_STREAM_CONFIG_ID`.
4. An Intron server API key from the Developers tab at `voice.intron.io`. `pcm` is the
   Pidgin-English code-switching model; `yo`, `ig`, and `ha` are also available.
5. A restricted Mapbox access token with Search Box API access.

For the required comparison benchmark, also bring an OpenAI API key for `whisper-1`
and an ElevenLabs API key for `scribe_v2`. Those keys are only used by the offline benchmark
runner, not by live rider guidance.

The exact Infobip account setup and event list are in
[docs/INFOBIP_SETUP.md](docs/INFOBIP_SETUP.md).

Set `DEMO_MODE=false` for live use. `GET /health` shows every missing production input.
Keep every credential server-side; none belongs in the rider app.

## Core API

The Dev 2 handoff is documented in [docs/API_CONTRACT.md](docs/API_CONTRACT.md). The
main endpoints are:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/deliveries` | Create a resolution session and reuse known guidance if available |
| `POST /v1/deliveries/{id}/proxy` | Reserve an order-scoped proxy number |
| `POST /v1/telephony/infobip/events` | Consume Calls API events and create the rider/customer Dialog |
| `WS /v1/telephony/infobip/media` | Receive raw Infobip PCM and stream it to Sahara |
| `POST /v1/telephony/inbound` | Optional Twilio inbound fallback |
| `WS /v1/media/{call_id}` | Optional Twilio media fallback |
| `WS /v1/deliveries/{id}/events` | Replay and push live rider events |
| `GET /v1/deliveries/{id}/guidance` | Recover current trail after reconnect |
| `POST /v1/deliveries/{id}/complete` | Save outcome/final GPS and update graph confidence |
| `GET /v1/resolve?destination_key=...` | Resolve a learned destination without a call |

## Benchmark

The JSONL corpus format keeps the same reference audio labels and compares transcript
outputs from Sahara and two other models. A sample is included:

```powershell
python -m app.benchmark benchmark/sample.jsonl
```

Replace the sample hypotheses with outputs from real audio. Do not claim comparative model
performance from the sample; it only verifies the metric pipeline.

To run all three providers on labeled audio, create a JSONL manifest whose `audio` path is
relative to the manifest:

```json
{"id":"yaba-001","audio":"audio/yaba-001.wav","reference":"Pass Mobil, take the second right.","landmarks":["mobil"],"relations":["pass","turn_right"]}
```

Then supply `INTRON_API_KEY`, `OPENAI_API_KEY`, and `ELEVENLABS_API_KEY` and run:

```powershell
python -m app.benchmark_run benchmark/corpus.jsonl
```

The runner writes raw hypotheses and a metric report under `artifacts/`. Sahara uses its
synchronous file API, Whisper uses OpenAI's audio transcription endpoint, and ElevenLabs uses
Scribe v2.

## Storage and deployment

When `DATABASE_URL` is set, Waymark uses PostgreSQL/Neon as its primary store and creates the
core tables on startup. Without it, local/demo mode falls back to durable SQLite at
`DATABASE_PATH`. [docs/postgis.sql](docs/postgis.sql) contains the later PostGIS spatial-index
upgrade and corridor-query pattern.

Build the container with `docker compose up --build`, or deploy the `Dockerfile` to a service
that preserves `/app/data`. Before a pilot, replace the local repository with Postgres/PostGIS,
move event fan-out to Redis, terminate HTTPS at the edge, and set a written audio-retention and
deletion policy. Raw call audio is not stored by this implementation.

## Verification

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app
```
