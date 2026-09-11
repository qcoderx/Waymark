# Twilio setup for Waymark

Waymark uses one Twilio Programmable Voice number as a temporary delivery proxy. The rider
calls that number, Waymark checks the active delivery mapping, returns TwiML to dial the
customer, and streams both call tracks to Sahara.

## 1. Environment

Set these values locally and in Render:

```dotenv
TELEPHONY_PROVIDER=twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_PROXY_NUMBERS=+1xxxxxxxxxx
TWILIO_VALIDATE_SIGNATURES=true
PUBLIC_BASE_URL=https://waymark-ei0p.onrender.com
DEMO_MODE=false
```

Keep the credentials server-side. `TWILIO_PROXY_NUMBERS` accepts multiple comma-separated
E.164 numbers when the service grows.

## 2. Incoming call webhook

In Twilio Console, open **Phone Numbers > Manage > Active numbers**, select the Waymark
number, and set **Voice configuration > A call comes in** to:

```text
Webhook
POST https://waymark-ei0p.onrender.com/v1/telephony/inbound
```

No separate media-stream configuration is required. The webhook response tells Twilio to
open `wss://waymark-ei0p.onrender.com/v1/media/{call_id}` and send both audio tracks.

## 3. Trial accounts

On a Twilio trial, verify both the rider and customer numbers in the Console before testing.
Trial calls are limited by Twilio's current country and recipient restrictions. Upgrade is
required when the trial restrictions prevent the desired call route.

## 4. Test flow

1. Create a delivery with rider and customer numbers in E.164 format.
2. Call `POST /v1/deliveries/{delivery_id}/proxy`.
3. Call the returned Twilio number from the exact rider number stored on the delivery.
4. Confirm the customer rings and check `/v1/deliveries/{delivery_id}/events/history`.

