from __future__ import annotations

import argparse
import asyncio
import json

from .config import Settings
from .telephony import InfobipTelephony


async def _run(name: str) -> None:
    settings = Settings.from_env()
    if not settings.public_base_url.startswith("https://"):
        raise SystemExit("PUBLIC_BASE_URL must be a public HTTPS URL before provisioning")
    result = await InfobipTelephony(settings).create_media_stream_config(name)
    config_id = result.get("id")
    if not config_id:
        raise RuntimeError("Infobip returned no media stream configuration ID")
    print(
        json.dumps(
            {
                "media_stream_config_id": config_id,
                "next_step": "Set INFOBIP_MEDIA_STREAM_CONFIG_ID to this value",
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Waymark's Infobip MEDIA_STREAMING configuration"
    )
    parser.add_argument("--name", default="waymark-sahara")
    args = parser.parse_args()
    asyncio.run(_run(args.name))


if __name__ == "__main__":
    main()
