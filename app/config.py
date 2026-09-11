from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


def _https_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = f"https://{value}"
    if not value.startswith("https://"):
        raise ValueError("provider base URLs must use HTTPS")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    app_host: str
    app_port: int
    log_level: str
    public_base_url: str
    cors_origins: tuple[str, ...]
    demo_mode: bool
    database_path: Path
    database_url: str | None
    redis_url: str | None
    guidance_confidence_threshold: float
    grounding_radius_meters: int
    landmark_freshness_half_life_days: int
    telephony_provider: str
    daily_api_key: str | None
    daily_domain: str | None
    daily_api_base_url: str
    daily_room_ttl_minutes: int
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    twilio_proxy_numbers: tuple[str, ...]
    twilio_validate_signatures: bool
    infobip_api_key: str | None
    infobip_base_url: str
    infobip_calls_configuration_id: str | None
    infobip_media_stream_config_id: str | None
    infobip_proxy_numbers: tuple[str, ...]
    infobip_webhook_username: str | None
    infobip_webhook_password: str | None
    infobip_media_stream_username: str | None
    infobip_media_stream_password: str | None
    call_disclosure: str
    stt_provider: str
    intron_api_key: str | None
    intron_stream_url: str
    intron_file_url: str
    intron_language: str
    intron_sample_rate: int
    map_provider: str
    mapbox_access_token: str | None
    mapbox_search_url: str
    mapbox_geocoding_url: str
    openai_api_key: str | None
    openai_base_url: str
    openai_transcription_model: str
    elevenlabs_api_key: str | None
    elevenlabs_base_url: str
    elevenlabs_stt_model: str
    care_agent_enabled: bool
    care_agent_model: str
    care_disclosure: str

    @classmethod
    def from_env(cls) -> "Settings":
        _load_local_env()
        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "8000")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            cors_origins=_csv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8081"),
            demo_mode=_bool("DEMO_MODE", True),
            database_path=Path(os.getenv("DATABASE_PATH", "./data/waymark.db")),
            database_url=os.getenv("DATABASE_URL") or None,
            redis_url=os.getenv("REDIS_URL") or None,
            guidance_confidence_threshold=float(
                os.getenv("GUIDANCE_CONFIDENCE_THRESHOLD", "0.68")
            ),
            grounding_radius_meters=int(os.getenv("GROUNDING_RADIUS_METERS", "2500")),
            landmark_freshness_half_life_days=int(
                os.getenv("LANDMARK_FRESHNESS_HALF_LIFE_DAYS", "180")
            ),
            telephony_provider=os.getenv("TELEPHONY_PROVIDER", "twilio"),
            daily_api_key=os.getenv("DAILY_API_KEY") or None,
            daily_domain=os.getenv("DAILY_DOMAIN") or None,
            daily_api_base_url=_https_url(
                os.getenv("DAILY_API_BASE_URL", "https://api.daily.co/v1")
            ),
            daily_room_ttl_minutes=int(os.getenv("DAILY_ROOM_TTL_MINUTES", "60")),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID") or None,
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN") or None,
            twilio_proxy_numbers=_csv("TWILIO_PROXY_NUMBERS"),
            twilio_validate_signatures=_bool("TWILIO_VALIDATE_SIGNATURES", True),
            infobip_api_key=os.getenv("INFOBIP_API_KEY") or None,
            infobip_base_url=_https_url(
                os.getenv("INFOBIP_BASE_URL", "https://api.infobip.com")
            ),
            infobip_calls_configuration_id=(
                os.getenv("INFOBIP_CALLS_CONFIGURATION_ID") or None
            ),
            infobip_media_stream_config_id=(
                os.getenv("INFOBIP_MEDIA_STREAM_CONFIG_ID") or None
            ),
            infobip_proxy_numbers=_csv("INFOBIP_PROXY_NUMBERS"),
            infobip_webhook_username=os.getenv("INFOBIP_WEBHOOK_USERNAME") or None,
            infobip_webhook_password=os.getenv("INFOBIP_WEBHOOK_PASSWORD") or None,
            infobip_media_stream_username=(
                os.getenv("INFOBIP_MEDIA_STREAM_USERNAME") or None
            ),
            infobip_media_stream_password=(
                os.getenv("INFOBIP_MEDIA_STREAM_PASSWORD") or None
            ),
            call_disclosure=os.getenv(
                "CALL_DISCLOSURE",
                "This call is processed by Waymark to provide live directions.",
            ),
            stt_provider=os.getenv("STT_PROVIDER", "sahara"),
            intron_api_key=os.getenv("INTRON_API_KEY") or None,
            intron_stream_url=os.getenv(
                "INTRON_STREAM_URL", "wss://infer.voice.intron.io/stt/v1/stream"
            ),
            intron_file_url=os.getenv(
                "INTRON_FILE_URL", "https://infer.voice.intron.io/file/v1/upload/sync"
            ),
            intron_language=os.getenv("INTRON_LANGUAGE", "pcm"),
            intron_sample_rate=int(os.getenv("INTRON_SAMPLE_RATE", "16000")),
            map_provider=os.getenv("MAP_PROVIDER", "mapbox"),
            mapbox_access_token=os.getenv("MAPBOX_ACCESS_TOKEN") or None,
            mapbox_search_url=os.getenv(
                "MAPBOX_SEARCH_URL",
                "https://api.mapbox.com/search/searchbox/v1/forward",
            ),
            mapbox_geocoding_url=os.getenv(
                "MAPBOX_GEOCODING_URL",
                "https://api.mapbox.com/search/geocode/v6/forward",
            ),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openai_transcription_model=os.getenv(
                "OPENAI_TRANSCRIPTION_MODEL", "whisper-1"
            ),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY") or None,
            elevenlabs_base_url=os.getenv(
                "ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1"
            ).rstrip("/"),
            elevenlabs_stt_model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2"),
            care_agent_enabled=_bool("CARE_AGENT_ENABLED", True),
            care_agent_model=os.getenv("CARE_AGENT_MODEL", "gpt-5.6-luna"),
            care_disclosure=os.getenv(
                "CARE_DISCLOSURE",
                "Waymark listens to this call to assist both participants "
                "and perform approved tasks.",
            ),
        )

    @property
    def websocket_base_url(self) -> str:
        if self.public_base_url.startswith("https://"):
            return "wss://" + self.public_base_url.removeprefix("https://")
        if self.public_base_url.startswith("http://"):
            return "ws://" + self.public_base_url.removeprefix("http://")
        return self.public_base_url

    def production_readiness(self) -> dict[str, bool]:
        checks = {
            "public_https_url": self.public_base_url.startswith("https://"),
            "sahara_credentials": bool(self.intron_api_key),
            "mapbox_credentials": bool(self.mapbox_access_token),
            "postgres_database": bool(self.database_url),
            "demo_mode_disabled": not self.demo_mode,
        }
        if self.telephony_provider == "daily":
            checks.update(
                {
                    "daily_credentials": bool(self.daily_api_key),
                    "daily_domain": bool(self.daily_domain),
                }
            )
        elif self.telephony_provider == "infobip":
            checks.update(
                {
                    "infobip_credentials": bool(self.infobip_api_key),
                    "infobip_calls_configuration": bool(
                        self.infobip_calls_configuration_id
                    ),
                    "infobip_media_stream_configuration": bool(
                        self.infobip_media_stream_config_id
                    ),
                    "proxy_numbers": bool(self.infobip_proxy_numbers),
                    "infobip_webhook_auth": bool(
                        self.infobip_webhook_username
                        and self.infobip_webhook_password
                    ),
                    "infobip_media_stream_auth": bool(
                        self.infobip_media_stream_username
                        and self.infobip_media_stream_password
                    ),
                }
            )
        else:
            checks.update(
                {
                    "twilio_credentials": bool(
                        self.twilio_account_sid and self.twilio_auth_token
                    ),
                    "proxy_numbers": bool(self.twilio_proxy_numbers),
                }
            )
        if self.care_agent_enabled:
            checks["openai_agent_credentials"] = bool(self.openai_api_key)
        return checks
