from __future__ import annotations

import json
import struct
import sys
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "care-demo-audio"
TTS_URL = "https://infer.voice.intron.io/tts/v1/generate"

SCENARIOS = {
    "banking": {
        "voices": {
            "agent": {
                "voice_language": "en",
                "voice_accent": "igbo",
                "voice_gender": "female",
            },
            "customer": {
                "voice_language": "en",
                "voice_accent": "yoruba",
                "voice_gender": "male",
            },
        },
        "turns": [
            ("customer", "Hello, my name is Amina Yusuf."),
            ("agent", "Good afternoon, Amina. How can I help you?"),
            ("customer", "Please check my available account balance."),
            ("agent", "Waymark, check Amina's account balance now."),
            ("customer", "My debit card is missing. Please freeze my card."),
            ("agent", "Waymark, prepare the card freeze for confirmation."),
            ("customer", "Please open a support case for my missing card."),
        ],
    },
    "telecom": {
        "voices": {
            "agent": {
                "voice_language": "en",
                "voice_accent": "yoruba",
                "voice_gender": "female",
            },
            "customer": {
                "voice_language": "en",
                "voice_accent": "igbo",
                "voice_gender": "male",
            },
        },
        "turns": [
            ("customer", "Hello, my name is Chidi Okafor."),
            ("agent", "Good afternoon, Chidi. How can I help you?"),
            ("customer", "Please check my current data balance and plan."),
            ("agent", "Waymark, check Chidi's data plan now."),
            ("customer", "My phone was stolen. Please suspend my line."),
            ("agent", "Waymark, prepare the line suspension for confirmation."),
            ("customer", "Please open a support case for the stolen phone."),
        ],
    },
}


def synthesize(
    client: httpx.Client,
    api_key: str,
    voice: dict[str, str],
    text: str,
    target: Path,
) -> None:
    response = client.post(
        TTS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"text": text, **voice, "output_audio_format": "wav"},
    )
    response.raise_for_status()
    payload = response.json()
    audio_url = payload.get("data", {}).get("audio_path")
    if not audio_url:
        raise RuntimeError(f"Sahara did not return audio: {payload}")
    audio_response = client.get(audio_url.replace("http://", "https://", 1))
    audio_response.raise_for_status()
    target.write_bytes(audio_response.content)


def read_wav(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as audio:
        params = audio.getparams()
        if params.nchannels != 1 or params.sampwidth != 2:
            raise RuntimeError("Sahara demo audio must be mono PCM16")
        return params, audio.readframes(params.nframes)


def write_wav(path: Path, params: wave._wave_params, frames: bytes) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setparams(params._replace(nframes=0))
        audio.writeframes(frames)


def mix_pcm16(first: bytes, second: bytes) -> bytes:
    count = min(len(first), len(second)) // 2
    left = struct.unpack(f"<{count}h", first[: count * 2])
    right = struct.unpack(f"<{count}h", second[: count * 2])
    mixed = [max(-32768, min(32767, a + b)) for a, b in zip(left, right)]
    return struct.pack(f"<{len(mixed)}h", *mixed)


def generate_scenario(
    client: httpx.Client, api_key: str, name: str, scenario: dict
) -> dict:
    scenario_out = OUT / name
    scenario_out.mkdir(parents=True, exist_ok=True)
    clips = []
    for index, (role, text) in enumerate(scenario["turns"], start=1):
        path = scenario_out / f"{index:02d}-{role}.wav"
        if not path.exists():
            synthesize(client, api_key, scenario["voices"][role], text, path)
        params, frames = read_wav(path)
        clips.append(
            {
                "role": role,
                "text": text,
                "path": path,
                "params": params,
                "frames": frames,
                "duration": len(frames) / (params.framerate * params.sampwidth),
            }
        )

    reference = clips[0]["params"]
    for clip in clips[1:]:
        current = clip["params"]
        if (current.nchannels, current.sampwidth, current.framerate) != (
            reference.nchannels,
            reference.sampwidth,
            reference.framerate,
        ):
            raise RuntimeError("Sahara returned inconsistent WAV formats")

    cursor = 2.0
    for clip in clips:
        clip["start"] = cursor
        cursor += clip["duration"] + 1.8
    # Keep enough trailing silence that Chrome does not loop the fake microphone
    # track while the call waits for Waymark's final action response.
    total_seconds = cursor + 35.0
    total_bytes = int(total_seconds * reference.framerate) * reference.sampwidth
    tracks = {role: bytearray(total_bytes) for role in ("agent", "customer")}
    for clip in clips:
        start = int(clip["start"] * reference.framerate) * reference.sampwidth
        tracks[clip["role"]][start : start + len(clip["frames"])] = clip["frames"]

    for role, frames in tracks.items():
        write_wav(scenario_out / f"{name}-demo-{role}.wav", reference, bytes(frames))
    conversation = mix_pcm16(bytes(tracks["agent"]), bytes(tracks["customer"]))
    write_wav(scenario_out / f"{name}-demo-conversation.wav", reference, conversation)

    manifest = {
        "provider": "Sahara TTS by Intron",
        "scenario": name,
        "sample_rate": reference.framerate,
        "duration_seconds": round(total_seconds, 2),
        "voices": scenario["voices"],
        "turns": [
            {
                "role": clip["role"],
                "start_seconds": round(clip["start"], 2),
                "duration_seconds": round(clip["duration"], 2),
                "text": clip["text"],
                "clip": clip["path"].name,
            }
            for clip in clips
        ],
    }
    (scenario_out / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    settings = Settings.from_env()
    if not settings.intron_api_key:
        raise SystemExit("INTRON_API_KEY is required")
    OUT.mkdir(parents=True, exist_ok=True)
    manifests = []
    with httpx.Client(timeout=150, trust_env=False, follow_redirects=True) as client:
        for name, scenario in SCENARIOS.items():
            manifests.append(
                generate_scenario(client, settings.intron_api_key, name, scenario)
            )
    print(json.dumps(manifests, indent=2))


if __name__ == "__main__":
    main()
