# Infobip setup for Waymark

Waymark uses the Infobip Calls API for temporary proxy numbers, two-party Dialogs,
and separate live audio streams for the rider and customer. The API key and base URL
are already represented in `.env`; the remaining values identify account resources.

Do not create a **Number Masking** configuration for this integration. That screen's
Callback URL and Status URL use the Number Masking contract and do not expose the Calls
API media-stream flow Waymark implements. Use **Voice and WebRTC → Calls API** instead.

## 1. Enable the account features

Ask Infobip to enable Calls API and Media Streaming on the account. The API key needs
the `calls:traffic:send` scope.

## 2. Create the Calls Configuration

Create a Calls Configuration in the Infobip portal and copy its ID into:

```dotenv
INFOBIP_CALLS_CONFIGURATION_ID=your-calls-configuration-id
```

Lease one or more voice-capable Infobip numbers. For every number, set its Voice action
to **Forward to subscription** and choose the same Calls Configuration. Put the numbers
in `.env` in E.164 format:

```dotenv
INFOBIP_PROXY_NUMBERS=+2342012345678,+2342098765432
```

## 3. Create the event subscription

Create a `VOICE_VIDEO` subscription scoped to the Calls Configuration. Send events to:

```text
POST https://your-public-host/v1/telephony/infobip/events
```

Subscribe to these events:

- `CALL_RECEIVED`
- `CALL_RINGING`
- `CALL_PRE_ESTABLISHED`
- `CALL_ESTABLISHED`
- `CALL_RECONNECTED`
- `CALL_DISCONNECTED`
- `CALL_FINISHED`
- `CALL_FAILED`
- `DIALOG_ESTABLISHED`
- `DIALOG_FINISHED`
- `DIALOG_FAILED`
- `SAY_FINISHED`

Configure Basic authentication on the notification profile with the values in
`INFOBIP_WEBHOOK_USERNAME` and `INFOBIP_WEBHOOK_PASSWORD`.

## 4. Create the media stream configuration

Set `PUBLIC_BASE_URL` to the public HTTPS origin. Then run:

```powershell
python -m app.infobip_setup
```

The command creates a `MEDIA_STREAMING` configuration targeting
`wss://your-public-host/v1/telephony/infobip/media`. It applies the Basic credentials
from `INFOBIP_MEDIA_STREAM_USERNAME` and `INFOBIP_MEDIA_STREAM_PASSWORD` and prints the
new configuration ID. Copy that ID into:

```dotenv
INFOBIP_MEDIA_STREAM_CONFIG_ID=your-media-stream-config-id
```

## 5. Enable live mode

Set `DEMO_MODE=false`, restart the service, and check `GET /health`. A live pilot is
ready when `production_ready` is `true` and every item under `checks` is `true`.

When the rider calls an assigned proxy number, Waymark validates the rider, creates an
Infobip Dialog to the customer, plays the disclosure to both parties, then starts a
separate non-replacing media stream for each call leg. The service resamples Infobip's
PCM16 audio for Sahara and labels the streams as rider and customer.
