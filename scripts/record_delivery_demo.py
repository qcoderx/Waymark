from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://waymark-t0zj.onrender.com"
OUT = ROOT / "output" / "demo-video"
RAW = OUT / "raw"
AUDIO = ROOT / "output" / "delivery-demo-audio"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())
VIEWPORT = {"width": 1366, "height": 900}
RECORD_AFTER_CONNECTED_SECONDS = 62
OUTPUT_DURATION_SECONDS = 52


def get_json_with_retry(
    client: httpx.Client, path: str, *, attempts: int = 4
) -> Any:
    response: httpx.Response | None = None
    for attempt in range(attempts):
        response = client.get(path)
        if response.status_code < 500:
            response.raise_for_status()
            return response.json()
        if attempt + 1 < attempts:
            time.sleep(3)
    assert response is not None
    response.raise_for_status()


def create_delivery() -> tuple[dict, dict]:
    with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as client:
        health = get_json_with_retry(client, "/health")
        if not health.get("production_ready"):
            raise RuntimeError("Waymark production is not ready")
        response = client.get(
            "/v1/maps/geocode", params={"query": "Oyingbo Market, Lagos, Nigeria"}
        )
        response.raise_for_status()
        matches = response.json()
        destination = next(
            (match for match in matches if match["name"] == "Oyingbo Street"),
            matches[0],
        )
        marker = int(time.time())
        response = client.post(
            "/v1/deliveries",
            json={
                "external_order_id": f"recorded-demo-{marker}",
                "rider_ref": "sahara-demo-rider",
                "rider_phone": "+2348000000001",
                "customer_ref": "sahara-demo-customer",
                "customer_phone": "+2348000000002",
                "coarse_location": destination["location"],
                "coarse_address": "Oyingbo Street, Ebute Metta, Lagos",
                "destination_key": f"recorded-ebute-metta-{marker}",
            },
        )
        response.raise_for_status()
        delivery = response.json()
        response = client.post(f"/v1/deliveries/{delivery['id']}/webrtc")
        response.raise_for_status()
        return delivery, response.json()


def compose_video(
    rider_video: Path,
    customer_video: Path,
    rider_trim: float,
    customer_trim: float,
) -> Path:
    output = OUT / "waymark-delivery-live-call-demo.mp4"
    duration = OUTPUT_DURATION_SECONDS
    conversation = AUDIO / "delivery-demo-conversation.wav"
    filter_graph = (
        f"[0:v]trim=start={rider_trim:.3f}:duration={duration},setpts=PTS-STARTPTS,"
        "crop=650:800:700:90,scale=700:862:flags=lanczos,"
        "pad=704:866:2:2:color=0xe9483f[rider];"
        f"[1:v]trim=start={customer_trim:.3f}:duration={duration},setpts=PTS-STARTPTS,"
        "crop=650:800:700:90,scale=700:862:flags=lanczos,"
        "pad=704:866:2:2:color=0xf7c94b[customer];"
        f"color=c=0xf7f4ef:s=1920x1080:d={duration}[base];"
        "[base][rider]overlay=238:150[tmp];"
        "[tmp][customer]overlay=978:150[video];"
        f"[2:a]adelay=3000,apad=pad_dur={duration},atrim=duration={duration},"
        "volume=1.15[audio]"
    )
    command = [
        str(FFMPEG),
        "-y",
        "-i",
        str(rider_video),
        "-i",
        str(customer_video),
        "-i",
        str(conversation),
        "-filter_complex",
        filter_graph,
        "-map",
        "[video]",
        "-map",
        "[audio]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-t",
        str(duration),
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-4000:])
    return output


async def record_call(delivery: dict, links: dict) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    profiles = {role: OUT / f"chrome-{role}" for role in ("rider", "customer")}
    for path in (*profiles.values(), RAW):
        if path.exists():
            shutil.rmtree(path)
    RAW.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    page_started: dict[str, float] = {}
    console: dict[str, list[str]] = {"rider": [], "customer": []}
    page_errors: dict[str, list[str]] = {"rider": [], "customer": []}
    async with async_playwright() as playwright:
        contexts = {}
        pages = {}
        videos = {}
        for role in ("rider", "customer"):
            context = await playwright.chromium.launch_persistent_context(
                str(profiles[role]),
                executable_path=str(CHROME),
                headless=True,
                viewport=VIEWPORT,
                permissions=["microphone", "geolocation"],
                geolocation={"latitude": 6.5536, "longitude": 3.3436},
                record_video_dir=str(RAW / role),
                record_video_size=VIEWPORT,
                args=[
                    "--use-fake-device-for-media-stream",
                    "--use-file-for-fake-audio-capture="
                    f"{AUDIO / ('delivery-demo-' + role + '.wav')}",
                    "--use-fake-ui-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--no-first-run",
                ],
            )
            context.set_default_timeout(60_000)
            page = context.pages[0] if context.pages else await context.new_page()
            page_started[role] = time.perf_counter()
            page.on(
                "console",
                lambda message, r=role: console[r].append(f"{message.type}: {message.text}"),
            )
            page.on("pageerror", lambda error, r=role: page_errors[r].append(str(error)))
            contexts[role] = context
            pages[role] = page
            videos[role] = page.video

        await asyncio.gather(
            pages["rider"].goto(links["rider_url"], wait_until="domcontentloaded"),
            pages["customer"].goto(links["customer_url"], wait_until="domcontentloaded"),
        )
        await asyncio.gather(
            pages["rider"].wait_for_function(
                "() => Boolean(window.Daily || window.DailyIframe)"
            ),
            pages["customer"].wait_for_function(
                "() => Boolean(window.Daily || window.DailyIframe)"
            ),
        )
        join_at = time.perf_counter()
        await asyncio.gather(
            pages["rider"].click("#primary"), pages["customer"].click("#primary")
        )
        await asyncio.gather(
            pages["rider"].wait_for_function(
                "document.querySelector('#status')?.textContent === 'Call connected'"
            ),
            pages["customer"].wait_for_function(
                "document.querySelector('#status')?.textContent === 'Call connected'"
            ),
        )
        connected_at = time.perf_counter()
        print(
            f"Both recorded participants connected after {connected_at - join_at:.1f}s",
            flush=True,
        )
        await asyncio.gather(
            pages["rider"].wait_for_timeout(RECORD_AFTER_CONNECTED_SECONDS * 1000),
            pages["customer"].wait_for_timeout(RECORD_AFTER_CONNECTED_SECONDS * 1000),
        )
        snapshots = {}
        for role, page in pages.items():
            snapshots[role] = await page.evaluate(
                """() => ({
                  status: document.querySelector('#status')?.textContent,
                  peer: document.querySelector('#other')?.textContent,
                  peer_state: document.querySelector('#peerState')?.textContent,
                  error: document.querySelector('#error')?.textContent,
                  guidance_title: document.querySelector('#guidanceTitle')?.textContent,
                  guidance_instruction: document.querySelector('#guidanceInstruction')?.textContent,
                  guidance_transcript: document.querySelector('#guidanceTranscript')?.textContent,
                  map_markers: document.querySelectorAll('#guidanceMap .mapboxgl-marker').length
                })"""
            )
        await pages["rider"].screenshot(path=str(OUT / "recording-final-rider.png"))
        await pages["customer"].screenshot(path=str(OUT / "recording-final-customer.png"))
        await asyncio.gather(*(context.close() for context in contexts.values()))
        video_paths = {role: Path(await videos[role].path()) for role in videos}

    trim = {
        role: max(0, join_at - page_started[role] - 1.0)
        for role in ("rider", "customer")
    }
    final_video = compose_video(
        video_paths["rider"], video_paths["customer"], trim["rider"], trim["customer"]
    )
    events: list[dict] = []
    guidance = None
    result_fetch_error = None
    await asyncio.sleep(10)
    try:
        with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as client:
            events = get_json_with_retry(
                client, f"/v1/deliveries/{delivery['id']}/events/history"
            )
            guidance = get_json_with_retry(
                client, f"/v1/deliveries/{delivery['id']}/guidance"
            )
    except (httpx.HTTPError, ValueError) as error:
        result_fetch_error = str(error)
    result = {
        "started_at": started_at.isoformat(),
        "delivery_id": delivery["id"],
        "video": str(final_video),
        "snapshots": snapshots,
        "event_counts": {
            event_type: sum(event["type"] == event_type for event in events)
            for event_type in sorted({event["type"] for event in events})
        },
        "transcripts": [
            {
                "speaker": event.get("data", {}).get("speaker"),
                "text": event.get("data", {}).get("transcript"),
            }
            for event in events
            if event["type"] == "transcript.final"
        ],
        "errors": [event for event in events if event["type"] == "error"],
        "guidance": guidance,
        "result_fetch_error": result_fetch_error,
        "console": console,
        "page_errors": page_errors,
    }
    (OUT / "recording-result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


async def main() -> None:
    if not CHROME.exists() or not FFMPEG.exists():
        raise SystemExit("Chrome and Playwright ffmpeg are required")
    for name in (
        "delivery-demo-rider.wav",
        "delivery-demo-customer.wav",
        "delivery-demo-conversation.wav",
    ):
        if not (AUDIO / name).exists():
            raise SystemExit(f"Missing Sahara audio: {name}")
    OUT.mkdir(parents=True, exist_ok=True)
    delivery, links = create_delivery()
    result = await record_call(delivery, links)
    print(
        json.dumps(
            {
                "video": result["video"],
                "delivery_id": result["delivery_id"],
                "event_counts": result["event_counts"],
                "errors": len(result["errors"]),
                "rider": result["snapshots"]["rider"],
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
