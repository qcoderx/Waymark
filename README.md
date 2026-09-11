# Waymark

Waymark is an action layer for two-sided conversations. It listens to both participants,
understands what they need, reads permitted organizational data, performs approved tasks, and
shares the result while they are still talking. Delivery navigation is the first vertical;
customer care and business operations are the second.

## What works

- Delivery sessions and temporary order-to-proxy mappings
- Daily private, audio-only WebRTC rooms with separate expiring rider/customer links
- A responsive Waymark call screen that sends each speaker's microphone to Sahara
- Bank, telecom, fintech, and business customer-care sessions backed by seeded sandbox data
- OpenAI Responses function tools for customer lookup, cases, account protection, and invoices
- Confirmation-gated sensitive actions with a durable action and conversation audit trail
- Downloadable PDF invoices persisted in PostgreSQL and visible to both call participants
- Twilio Programmable Voice rider/customer bridging and two-track Media Streams
- Twilio signature verification and 8 kHz mu-law to 16 kHz Sahara conversion
- Optional Infobip Calls API and per-leg media-streaming integration
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

1. A public HTTPS URL for this service. Waymark derives secure call and WebSocket URLs from
   `PUBLIC_BASE_URL`.
2. A Daily API key and domain. Set `TELEPHONY_PROVIDER=daily`; Waymark creates a private,
   two-person, audio-only room for each delivery and returns expiring rider/customer links.
3. An Intron server API key from the Developers tab at `voice.intron.io`. `pcm` is the
   Pidgin-English code-switching model; `yo`, `ig`, and `ha` are also available.
4. A restricted Mapbox access token with Search Box API access.

The customer-care action agent uses the OpenAI API key. The same key can run `whisper-1` in
the comparison benchmark; an ElevenLabs key enables the `scribe_v2` benchmark provider.
Neither provider is used by live delivery guidance.

The browser-call setup is in [docs/DAILY_SETUP.md](docs/DAILY_SETUP.md). Twilio and Infobip
remain documented as optional phone-network providers in [docs/TWILIO_SETUP.md](docs/TWILIO_SETUP.md)
and [docs/INFOBIP_SETUP.md](docs/INFOBIP_SETUP.md).

Set `DEMO_MODE=false` for live use. `GET /health` shows every missing production input.
Keep every credential server-side; none belongs in the rider app.

## Core API

The Dev 2 handoff is documented in [docs/API_CONTRACT.md](docs/API_CONTRACT.md). The
main endpoints are:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/deliveries` | Create a resolution session and reuse known guidance if available |
| `POST /v1/deliveries/{id}/webrtc` | Create private Daily rider/customer call links |
| `GET /call/{id}#role=...&access=...` | Open the Waymark browser call screen |
| `WS /v1/deliveries/{id}/daily-audio/{role}` | Stream labeled browser microphone audio to Sahara |
| `POST /v1/deliveries/{id}/proxy` | Reserve a proxy number when using Twilio or Infobip |
| `POST /v1/telephony/inbound` | Receive Twilio calls and return bridge/stream TwiML |
| `WS /v1/media/{call_id}` | Receive both Twilio audio tracks and stream them to Sahara |
| `POST /v1/telephony/infobip/events` | Optional Infobip Calls API events |
| `WS /v1/telephony/infobip/media` | Optional Infobip PCM media stream |
| `WS /v1/deliveries/{id}/events` | Replay and push live rider events |
| `GET /v1/deliveries/{id}/guidance` | Recover current trail after reconnect |
| `POST /v1/deliveries/{id}/complete` | Save outcome/final GPS and update graph confidence |
| `GET /v1/resolve?destination_key=...` | Resolve a learned destination without a call |
| `GET /v1/care/customers` | Search seeded bank, telecom, fintech, and business customers |
| `POST /v1/care/sessions` | Start a general action-oriented conversation |
| `POST /v1/care/sessions/{id}/webrtc` | Create two-sided customer-care or business call links |
| `POST /v1/care/sessions/{id}/turns` | Process a labeled utterance and execute selected tools |
| `GET /v1/care/sessions/{id}` | Retrieve the shared timeline, actions, and artifacts |
| `POST /v1/care/actions/{id}/confirm` | Confirm a pending sensitive action |
| `GET /v1/care/artifacts/{id}/download` | Download a generated conversation artifact |

The generalized platform boundary and vertical contract are documented in
[docs/PLATFORM_SCOPE.md](docs/PLATFORM_SCOPE.md). A runnable walkthrough is in
[docs/CARE_DEMO.md](docs/CARE_DEMO.md).

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
