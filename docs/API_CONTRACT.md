# Waymark API contract

This contract is the handoff boundary between Dev 1 and Dev 2. The OpenAPI schema at
`/docs` remains authoritative for full field definitions.

## Rider flow

1. `POST /v1/deliveries` creates a resolution session. Phone values use E.164 format;
   `customer_ref` and `rider_ref` are opaque platform identifiers.
2. `POST /v1/deliveries/{id}/webrtc` creates a private Daily room and returns separate
   expiring `rider_url` and `customer_url` values.
3. Send the customer URL to the customer and open the rider URL in the rider app or browser.
4. `WS /v1/deliveries/{id}/events` replays prior events, then streams new ones.
5. `GET /v1/deliveries/{id}/guidance` recovers current state after reconnect.
6. `POST /v1/deliveries/{id}/complete` sends final GPS and outcome.

With `TELEPHONY_PROVIDER=daily`, each URL opens Waymark's audio-only call screen. The screen
exchanges its browser-only fragment grant for a room-scoped Daily token, joins the two-person room,
and streams local PCM audio to `WS /v1/deliveries/{id}/daily-audio/{role}` for Sahara. The
Daily API key never reaches the browser.

With `TELEPHONY_PROVIDER=twilio`, `POST /v1/deliveries/{id}/proxy` reserves a number. Twilio posts the incoming call to
`POST /v1/telephony/inbound`; Waymark's TwiML bridges the customer and opens the
two-track media stream at `WS /v1/media/{call_id}`.

With `TELEPHONY_PROVIDER=infobip`, Infobip posts Calls API lifecycle events to
`POST /v1/telephony/infobip/events` and streams PCM16 audio to
`WS /v1/telephony/infobip/media`. These are provider callbacks; Dev 2 does not call them.

Creating a session with a `destination_key` that Waymark already learned immediately
stores and emits a `route.reused` guidance event.

## Customer-care and business flow

1. Select a seeded sandbox customer with `GET /v1/care/customers?vertical=banking`.
2. Create a session with `POST /v1/care/sessions` and attach the customer when applicable.
3. Create two private participant links with `POST /v1/care/sessions/{id}/webrtc`.
4. Each call page streams labeled audio to Sahara in short segments. Final segments are sent
   to the action agent while the conversation remains active.
5. The agent uses OpenAI Responses function tools to read sandbox data or execute permitted
   actions. If OpenAI is unavailable, deterministic handling keeps the core demo operational.
6. Both pages poll `GET /v1/care/sessions/{id}` and display the latest Waymark response and
   downloadable artifacts.
7. `freeze_card` and `suspend_line` remain `pending_confirmation` until
   `POST /v1/care/actions/{id}/confirm` receives their per-action confirmation token.

The initial tool catalog contains `lookup_customer`, `get_customer_profile`,
`open_support_case`, `freeze_card`, `suspend_line`, and `create_invoice`. Transfers, refunds,
loans, and identity changes are deliberately outside the sandbox executor.

## Frozen event envelope

```json
{
  "id": "evt_...",
  "type": "guidance.updated",
  "delivery_id": "del_...",
  "call_id": "call_...",
  "trace_id": "trace_...",
  "timestamp": "2026-09-10T16:00:00Z",
  "data": {}
}
```

| Event | Meaning | Key data |
| --- | --- | --- |
| `call.status` | Call lifecycle changed | `status`, `provider` |
| `transcript.partial` | Replace the temporary transcript line | `transcript` |
| `transcript.final` | Append a stable utterance | `transcript`, `speaker`, `confidence` |
| `landmark.detected` | A spoken landmark and its top candidate | `landmark`, `top_candidate` |
| `guidance.updated` | Render a confident ordered trail | `guidance`, `processing_latency_ms` |
| `guidance.uncertain` | Show a visual-confirmation state | `guidance`, `processing_latency_ms` |
| `delivery.completed` | Delivery outcome saved | outcome fields |
| `route.learned` | Successful arrival promoted observations | `destination_key`, `confidence` |
| `route.reused` | A later session received graph guidance | `guidance` |
| `error` | A pipeline stage failed | `stage`, provider detail |

The socket emits `{"type":"system.ping"}` during idle periods. Clients may ignore it.

## Demo controls

With `DEMO_MODE=true`, `POST /v1/demo/deliveries/{id}/utterances` injects a final
transcript through the real extraction, grounding, persistence, and event pipeline.
`POST /v1/demo/run` performs the complete learn-and-reuse story in one call.
