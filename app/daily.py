from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


class DailyAPIError(RuntimeError):
    pass


CALL_ROLES = {"rider", "customer", "agent", "employee", "counterparty"}


@dataclass(frozen=True, slots=True)
class DailyAccess:
    delivery_id: str
    call_id: str
    role: str
    expires_at: int


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_access_token(
    secret: str, delivery_id: str, call_id: str, role: str, expires_at: int
) -> str:
    if role not in CALL_ROLES:
        raise ValueError("unsupported call role")
    payload = _b64encode(
        json.dumps(
            {"d": delivery_id, "c": call_id, "r": role, "e": expires_at},
            separators=(",", ":"),
        ).encode()
    )
    signature = _b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def validate_access_token(secret: str, token: str) -> DailyAccess | None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64encode(
            hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_b64decode(payload))
        role = str(data["r"])
        expires_at = int(data["e"])
        if role not in CALL_ROLES or expires_at <= int(time.time()):
            return None
        return DailyAccess(
            delivery_id=str(data["d"]),
            call_id=str(data["c"]),
            role=role,
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class DailyClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.daily_api_key and self.settings.daily_domain)

    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.daily_api_key:
            raise DailyAPIError("DAILY_API_KEY is not configured")
        return {
            "Authorization": f"Bearer {self.settings.daily_api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def room_name(delivery_id: str) -> str:
        safe_id = "".join(char for char in delivery_id if char.isalnum() or char in "-_")
        return f"waymark-{safe_id}"[:128]

    async def ensure_room(self, delivery_id: str, expires_at: int) -> dict[str, Any]:
        name = self.room_name(delivery_id)
        body = {
            "name": name,
            "privacy": "private",
            "properties": {
                "exp": expires_at,
                "eject_at_room_exp": True,
                "max_participants": 2,
                "start_video_off": True,
                "start_audio_off": False,
                "enable_screenshare": False,
                "enable_chat": False,
                "permissions": {"canSend": ["audio"]},
            },
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.settings.daily_api_base_url}/rooms", headers=self.headers, json=body
            )
            if response.status_code in {400, 409}:
                response = await client.get(
                    f"{self.settings.daily_api_base_url}/rooms/{name}", headers=self.headers
                )
            if response.is_error:
                raise DailyAPIError(
                    f"Daily room request failed ({response.status_code}): {response.text[:300]}"
                )
            room = response.json()
        expected_host = f"{self.settings.daily_domain}.daily.co"
        if httpx.URL(room.get("url", "")).host != expected_host:
            raise DailyAPIError("Daily returned a room outside the configured domain")
        return room

    async def create_meeting_token(
        self, room_name: str, role: str, user_ref: str, expires_at: int
    ) -> str:
        body = {
            "properties": {
                "room_name": room_name,
                "user_name": {
                    "rider": "Rider",
                    "customer": "Customer",
                    "agent": "Support agent",
                    "employee": "Business",
                    "counterparty": "Counterparty",
                }.get(role, "Participant"),
                "user_id": user_ref[:36],
                "exp": expires_at,
                "eject_at_token_exp": True,
                "start_video_off": True,
                "start_audio_off": False,
                "enable_screenshare": False,
                "permissions": {"canSend": ["audio"]},
            }
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.settings.daily_api_base_url}/meeting-tokens",
                headers=self.headers,
                json=body,
            )
        if response.is_error:
            raise DailyAPIError(
                f"Daily token request failed ({response.status_code}): {response.text[:300]}"
            )
        return str(response.json()["token"])
