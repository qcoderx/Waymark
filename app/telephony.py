from __future__ import annotations

import base64
import hashlib
import hmac
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

from .config import Settings


def validate_twilio_signature(
    url: str, params: dict[str, str], signature: str | None, auth_token: str | None
) -> bool:
    if not signature or not auth_token:
        return False
    material = url + "".join(key + str(params[key]) for key in sorted(params))
    digest = hmac.new(auth_token.encode(), material.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


class TwilioTelephony:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def inbound_twiml(self, call_id: str, customer_phone: str) -> str:
        response = Element("Response")
        SubElement(response, "Say").text = self.settings.call_disclosure
        start = SubElement(response, "Start")
        stream = SubElement(
            start,
            "Stream",
            {
                "name": f"waymark-{call_id}",
                "url": f"{self.settings.websocket_base_url}/v1/media/{call_id}",
                "track": "both_tracks",
                "statusCallback": (
                    f"{self.settings.public_base_url}/v1/telephony/stream-status"
                ),
            },
        )
        SubElement(stream, "Parameter", {"name": "call_id", "value": call_id})
        dial = SubElement(
            response,
            "Dial",
            {
                "answerOnBridge": "true",
                "timeout": "25",
                "action": f"{self.settings.public_base_url}/v1/telephony/call-status",
                "method": "POST",
            },
        )
        SubElement(dial, "Number").text = customer_phone
        return '<?xml version="1.0" encoding="UTF-8"?>' + tostring(
            response, encoding="unicode", short_empty_elements=True
        )


def validate_basic_authorization(
    authorization: str | None, username: str | None, password: str | None
) -> bool:
    if not authorization or not username or not password:
        return False
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(supplied, f"{username}:{password}")


def infobip_phone(number: str) -> str:
    """Infobip PHONE endpoints use E.164 digits without a leading plus."""

    return number.strip().removeprefix("+")


class InfobipTelephony:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def _headers(self) -> dict[str, str]:
        if not self.settings.infobip_api_key:
            raise RuntimeError("INFOBIP_API_KEY is required")
        return {
            "Authorization": f"App {self.settings.infobip_api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def create_dialog(
        self,
        parent_call_id: str,
        customer_phone: str,
        caller_id: str,
        waymark_call_id: str,
    ) -> dict:
        payload = {
            "parentCallId": parent_call_id,
            "childCallRequest": {
                "endpoint": {
                    "type": "PHONE",
                    "phoneNumber": infobip_phone(customer_phone),
                },
                "from": infobip_phone(caller_id),
                "connectTimeout": 25,
                "customData": {"waymarkCallId": waymark_call_id},
            },
            "maxDuration": 3600,
        }
        async with httpx.AsyncClient(
            base_url=self.settings.infobip_base_url, timeout=15.0
        ) as client:
            response = await client.post(
                "/calls/1/dialogs", headers=self._headers, json=payload
            )
            response.raise_for_status()
            return response.json()

    async def create_media_stream_config(self, name: str = "waymark-sahara") -> dict:
        username = self.settings.infobip_media_stream_username
        password = self.settings.infobip_media_stream_password
        if not username or not password:
            raise RuntimeError("Infobip media stream Basic credentials are required")
        payload = {
            "type": "MEDIA_STREAMING",
            "name": name,
            "url": (
                f"{self.settings.websocket_base_url}"
                "/v1/telephony/infobip/media"
            ),
            "securityConfig": {
                "type": "BASIC",
                "username": username,
                "password": password,
            },
        }
        async with httpx.AsyncClient(
            base_url=self.settings.infobip_base_url, timeout=15.0
        ) as client:
            response = await client.post(
                "/calls/1/media-stream-configs",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def start_media_stream(self, provider_call_id: str) -> dict:
        config_id = self.settings.infobip_media_stream_config_id
        if not config_id:
            raise RuntimeError("INFOBIP_MEDIA_STREAM_CONFIG_ID is required")
        payload = {
            "mediaStream": {
                "audioProperties": {
                    "mediaStreamConfigId": config_id,
                    "replaceMedia": False,
                }
            }
        }
        async with httpx.AsyncClient(
            base_url=self.settings.infobip_base_url, timeout=15.0
        ) as client:
            response = await client.post(
                f"/calls/1/calls/{provider_call_id}/start-media-stream",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json() if response.content else {}

    async def say_dialog(self, provider_dialog_id: str, text: str) -> dict:
        payload = {"text": text, "language": "en", "speechRate": 1.0, "loopCount": 1}
        async with httpx.AsyncClient(
            base_url=self.settings.infobip_base_url, timeout=15.0
        ) as client:
            response = await client.post(
                f"/calls/1/dialogs/{provider_dialog_id}/say",
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json() if response.content else {}
