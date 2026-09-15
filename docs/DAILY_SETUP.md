# Daily browser-call setup

Daily replaces the paid phone-network bridge with a private browser audio call. Both people
need a data connection and must open their own Waymark link. No Daily API key is placed in a
URL or browser response.

## Render environment

Add these variables to the same Render web service:

```env
TELEPHONY_PROVIDER=daily
DAILY_API_KEY=your_daily_server_api_key
DAILY_DOMAIN=qcoderx
DAILY_API_BASE_URL=https://api.daily.co/v1
DAILY_ROOM_TTL_MINUTES=60
PUBLIC_BASE_URL=https://waymark-ei0p.onrender.com
DEMO_MODE=false
```

Keep the existing `DATABASE_URL`, `INTRON_API_KEY`, `MAPBOX_ACCESS_TOKEN`, and their current
provider settings. Save the variables and deploy the latest code. Twilio and Infobip variables
may remain present; Waymark ignores them while the provider is `daily`.

`GET /health` should then include:

```json
{
  "production_ready": true,
  "checks": {
    "public_https_url": true,
    "sahara_credentials": true,
    "mapbox_credentials": true,
    "postgres_database": true,
    "demo_mode_disabled": true,
    "daily_credentials": true,
    "daily_domain": true,
    "database_reachable": true
  }
}
```

## Create the first call

Create a delivery as usual, then call:

```http
POST /v1/deliveries/{delivery_id}/webrtc
```

The response contains `rider_url`, `customer_url`, and `expires_at`. Open the rider URL on the
rider's phone and send the customer URL to the customer. Both tap **Join call** and allow
microphone access. The links stop working after the configured room lifetime.

The access grant is kept in the browser-only URL fragment, so it is not included in ordinary
server access logs. The page calls the protected join endpoint itself and receives an HTTP-only
socket cookie. Do not extract or send Daily meeting tokens from application code. Treat both
role URLs as private delivery data.

## Operational behavior

- Rooms are private, limited to audio, and expire automatically.
- Rider and customer microphone streams use different role grants, preserving speaker labels.
- Raw audio is forwarded to Sahara and is not stored by Waymark.
- `WS /v1/deliveries/{delivery_id}/events` continues to provide transcript, landmark, and
  guidance updates to the rider experience.
- `POST /v1/deliveries/{delivery_id}/proxy` returns `409` while Daily is active because no
  phone number is required.
