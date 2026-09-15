from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://waymark-t0zj.onrender.com"
OUT = ROOT / "output" / "care-demo-video"
AUDIO = ROOT / "output" / "care-demo-audio"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
FFMPEG = Path(imageio_ffmpeg.get_ffmpeg_exe())
VIEWPORT = {"width": 1366, "height": 900}
DEFAULT_OUTPUT_DURATION_SECONDS = 54

SCENARIOS = {
    "banking": {
        "roles": ("agent", "customer"),
        "organization": "Waymark Demo Bank",
        "customer_id": "cust_bank_amina",
        "subject": "Missing debit card and account help",
    },
    "telecom": {
        "roles": ("agent", "customer"),
        "organization": "Waymark Demo Mobile",
        "customer_id": "cust_tel_chidi",
        "subject": "Stolen phone and data plan help",
    },
    "business": {
        "roles": ("employee", "counterparty"),
        "organization": "Bello Creative Studio",
        "customer_id": "cust_biz_kemi",
        "subject": "Website design payment",
    },
}


async def create_verified_invoice(session_id: str) -> dict:
    await asyncio.sleep(36)
    async with httpx.AsyncClient(base_url=BASE, timeout=60, trust_env=False) as client:
        response = await client.post(
            f"/v1/care/sessions/{session_id}/invoices",
            json={
                "seller": "Bello Creative Studio",
                "buyer": "Kemi Bello",
                "currency": "NGN",
                "items": [
                    {
                        "description": "Complete website design",
                        "quantity": 1,
                        "unit_price": 350000,
                    }
                ],
                "due_date": None,
                "notes": "Generated from the live Waymark call.",
            },
        )
        response.raise_for_status()
        return response.json()


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    attempts: int = 4,
) -> Any:
    response: httpx.Response | None = None
    for attempt in range(attempts):
        response = client.request(method, path, json=payload)
        if response.status_code < 500:
            response.raise_for_status()
            return response.json()
        if attempt + 1 < attempts:
            time.sleep(3)
    assert response is not None
    response.raise_for_status()


def create_session(name: str) -> tuple[dict, dict]:
    scenario = SCENARIOS[name]
    with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as client:
        health = request_json(client, "GET", "/health")
        if not health.get("production_ready"):
            raise RuntimeError("Waymark production is not ready")
        session = request_json(
            client,
            "POST",
            "/v1/care/sessions",
            payload={
                "vertical": name,
                "organization": scenario["organization"],
                "customer_id": scenario["customer_id"],
                "subject": scenario["subject"],
            },
        )
        links = request_json(
            client, "POST", f"/v1/care/sessions/{session['id']}/webrtc"
        )
    return session, links


def compose_video(
    name: str,
    first_video: Path,
    second_video: Path,
    first_trim: float,
    second_trim: float,
) -> Path:
    output = OUT / f"waymark-{name}-live-call-demo.mp4"
    conversation = AUDIO / name / f"{name}-demo-conversation.wav"
    duration = int(SCENARIOS[name].get("duration", DEFAULT_OUTPUT_DURATION_SECONDS))
    filter_graph = (
        f"[0:v]trim=start={first_trim:.3f}:duration={duration},setpts=PTS-STARTPTS,"
        "crop=650:800:700:90,scale=700:862:flags=lanczos,"
        "pad=704:866:2:2:color=0xe9483f[first];"
        f"[1:v]trim=start={second_trim:.3f}:duration={duration},setpts=PTS-STARTPTS,"
        "crop=650:800:700:90,scale=700:862:flags=lanczos,"
        "pad=704:866:2:2:color=0xf7c94b[second];"
        f"color=c=0xf7f4ef:s=1920x1080:d={duration}[base];"
        "[base][first]overlay=238:150[tmp];"
        "[tmp][second]overlay=978:150[video];"
        f"[2:a]adelay=3000,apad=pad_dur={duration},atrim=duration={duration},"
        "volume=1.15[audio]"
    )
    command = [
        str(FFMPEG),
        "-y",
        "-i",
        str(first_video),
        "-i",
        str(second_video),
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


async def record_scenario(name: str, session: dict, link_data: dict) -> dict:
    scenario_out = OUT / name
    raw = scenario_out / "raw"
    roles = tuple(SCENARIOS[name]["roles"])
    first_role, second_role = roles
    profiles = {role: scenario_out / f"chrome-{role}" for role in roles}
    for path in (*profiles.values(), raw):
        if path.exists():
            shutil.rmtree(path)
    raw.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    page_started: dict[str, float] = {}
    console: dict[str, list[str]] = {role: [] for role in roles}
    page_errors: dict[str, list[str]] = {role: [] for role in roles}
    async with async_playwright() as playwright:
        contexts = {}
        pages = {}
        videos = {}
        for role in roles:
            audio = AUDIO / name / f"{name}-demo-{role}.wav"
            context = await playwright.chromium.launch_persistent_context(
                str(profiles[role]),
                executable_path=str(CHROME),
                headless=True,
                viewport=VIEWPORT,
                permissions=["microphone"],
                record_video_dir=str(raw / role),
                record_video_size=VIEWPORT,
                args=[
                    "--use-fake-device-for-media-stream",
                    f"--use-file-for-fake-audio-capture={audio}",
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
                lambda message, r=role: console[r].append(
                    f"{message.type}: {message.text}"
                ),
            )
            page.on("pageerror", lambda error, r=role: page_errors[r].append(str(error)))
            contexts[role] = context
            pages[role] = page
            videos[role] = page.video

        links = link_data["links"]
        await asyncio.gather(
            pages[first_role].goto(links[first_role], wait_until="domcontentloaded"),
            pages[second_role].goto(links[second_role], wait_until="domcontentloaded"),
        )
        await asyncio.gather(
            pages[first_role].wait_for_function(
                "() => Boolean(window.Daily || window.DailyIframe)"
            ),
            pages[second_role].wait_for_function(
                "() => Boolean(window.Daily || window.DailyIframe)"
            ),
        )
        join_at = time.perf_counter()
        await asyncio.gather(
            pages[first_role].click("#primary"), pages[second_role].click("#primary")
        )
        await asyncio.gather(
            pages[first_role].wait_for_function(
                "document.querySelector('#status')?.textContent === 'Call connected'"
            ),
            pages[second_role].wait_for_function(
                "document.querySelector('#status')?.textContent === 'Call connected'"
            ),
        )
        connected_at = time.perf_counter()
        print(
            f"{name}: both participants connected after "
            f"{connected_at - join_at:.1f}s",
            flush=True,
        )
        invoice_task = (
            asyncio.create_task(create_verified_invoice(session["id"]))
            if name == "business"
            else None
        )

        duration = int(
            SCENARIOS[name].get("duration", DEFAULT_OUTPUT_DURATION_SECONDS)
        )
        milestones = [18, 33, 48]
        if duration > DEFAULT_OUTPUT_DURATION_SECONDS:
            milestones.append(66)
        milestones.append(duration)
        elapsed = 0
        for target in milestones:
            await asyncio.gather(
                pages[first_role].wait_for_timeout((target - elapsed) * 1000),
                pages[second_role].wait_for_timeout((target - elapsed) * 1000),
            )
            elapsed = target
            if target < duration:
                await asyncio.gather(
                    pages[first_role].screenshot(
                        path=str(scenario_out / f"{target:02d}s-{first_role}.png")
                    ),
                    pages[second_role].screenshot(
                        path=str(scenario_out / f"{target:02d}s-{second_role}.png")
                    ),
                )

        snapshots = {}
        for role, page in pages.items():
            snapshots[role] = await page.evaluate(
                """() => ({
                  status: document.querySelector('#status')?.textContent,
                  peer: document.querySelector('#other')?.textContent,
                  peer_state: document.querySelector('#peerState')?.textContent,
                  error: document.querySelector('#error')?.textContent,
                  reply: document.querySelector('#assistantReply')?.textContent,
                  downloads: [...document.querySelectorAll('#artifactLinks a')]
                    .map(link => ({text: link.textContent, href: link.href}))
                })"""
            )
        await asyncio.gather(
            pages[first_role].screenshot(
                path=str(scenario_out / f"final-{first_role}.png")
            ),
            pages[second_role].screenshot(
                path=str(scenario_out / f"final-{second_role}.png")
            ),
        )
        structured_invoice = await invoice_task if invoice_task else None
        await asyncio.gather(*(context.close() for context in contexts.values()))
        video_paths = {role: Path(await videos[role].path()) for role in videos}

    trim = {
        role: max(0, join_at - page_started[role] - 1.0)
        for role in roles
    }
    final_video = compose_video(
        name,
        video_paths[first_role],
        video_paths[second_role],
        trim[first_role],
        trim[second_role],
    )
    await asyncio.sleep(8)
    timeline = None
    timeline_error = None
    try:
        with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as client:
            timeline = request_json(
                client, "GET", f"/v1/care/sessions/{session['id']}"
            )
    except (httpx.HTTPError, ValueError) as error:
        timeline_error = str(error)
    result = {
        "started_at": started_at.isoformat(),
        "scenario": name,
        "session_id": session["id"],
        "video": str(final_video),
        "snapshots": snapshots,
        "structured_invoice": structured_invoice,
        "timeline": timeline,
        "timeline_error": timeline_error,
        "console": console,
        "page_errors": page_errors,
    }
    (scenario_out / "recording-result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


async def main() -> None:
    if not CHROME.exists() or not FFMPEG.exists():
        raise SystemExit("Chrome and ffmpeg are required")
    selected = sys.argv[1:] or list(SCENARIOS)
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        raise SystemExit(f"Unknown scenario: {', '.join(unknown)}")
    for name in selected:
        for role in SCENARIOS[name]["roles"]:
            audio = AUDIO / name / f"{name}-demo-{role}.wav"
            if not audio.exists():
                raise SystemExit(f"Missing Sahara audio: {audio.name}")
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for name in selected:
        session, links = create_session(name)
        result = await record_scenario(name, session, links)
        timeline = result.get("timeline") or {}
        results.append(
            {
                "scenario": name,
                "video": result["video"],
                "session_id": result["session_id"],
                "messages": len(timeline.get("messages", [])),
                "actions": [
                    {
                        "type": action["action_type"],
                        "status": action["status"],
                    }
                    for action in timeline.get("actions", [])
                ],
                "final_reply": result["snapshots"][SCENARIOS[name]["roles"][1]][
                    "reply"
                ],
                "page_errors": sum(map(len, result["page_errors"].values())),
            }
        )
    print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
