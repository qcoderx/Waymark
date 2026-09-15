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
OUT = ROOT / "output" / "delivery-demo-audio"
TTS_URL = "https://infer.voice.intron.io/tts/v1/generate"

VOICES = {
    "rider": {"voice_language": "en", "voice_accent": "yoruba", "voice_gender": "male"},
    "customer": {
        "voice_language": "en",
        "voice_accent": "igbo",
        "voice_gender": "female",
    },
}

TURNS = [
    ("rider", "Hello. I am currently in Oshodi, heading to Ebute Metta."),
    ("customer", "When you enter Ebute Metta, pass the Mobil filling station."),
    ("customer", "Continue straight towards the church."),
    ("rider", "I can see the church. Should I take the first left?"),
    ("customer", "Yes. Wait, no. Do not take that left."),
    ("customer", "Continue straight and take the next right after the church."),
    ("customer", "Look for the black gate opposite Mama Titi's shop. Na there I dey."),
]


def synthesize(
    client: httpx.Client,
    api_key: str,
    role: str,
    text: str,
    target: Path,
) -> None:
    response = client.post(
        TTS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "text": text,
            **VOICES[role],
            "output_audio_format": "wav",
        },
    )
    response.raise_for_status()
    payload = response.json()
    audio_url = payload.get("data", {}).get("audio_path")
    if not audio_url:
        raise RuntimeError(f"Sahara did not return audio for {role}: {payload}")
    audio_response = client.get(audio_url.replace("http://", "https://", 1))
    audio_response.raise_for_status()
    target.write_bytes(audio_response.content)


def read_wav(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as audio:
        params = audio.getparams()
        if params.nchannels != 1 or params.sampwidth != 2:
            raise RuntimeError(
                f"Expected mono PCM16 from Sahara, got {params.nchannels} channel(s), "
                f"{params.sampwidth * 8}-bit audio"
            )
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


def main() -> None:
    settings = Settings.from_env()
    if not settings.intron_api_key:
        raise SystemExit("INTRON_API_KEY is required")
    OUT.mkdir(parents=True, exist_ok=True)

    clips = []
    with httpx.Client(timeout=150, trust_env=False, follow_redirects=True) as client:
        for index, (role, text) in enumerate(TURNS, start=1):
            path = OUT / f"{index:02d}-{role}.wav"
            synthesize(client, settings.intron_api_key, role, text, path)
            params, frames = read_wav(path)
            clips.append(
                {
                    "index": index,
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
        if (
            current.nchannels,
            current.sampwidth,
            current.framerate,
        ) != (reference.nchannels, reference.sampwidth, reference.framerate):
            raise RuntimeError("Sahara returned inconsistent WAV formats")

    cursor = 2.0
    for clip in clips:
        clip["start"] = cursor
        cursor += clip["duration"] + 1.7
    total_seconds = cursor + 2.0
    total_bytes = int(total_seconds * reference.framerate) * reference.sampwidth
    tracks = {role: bytearray(total_bytes) for role in VOICES}

    for clip in clips:
        start = int(clip["start"] * reference.framerate) * reference.sampwidth
        tracks[clip["role"]][start : start + len(clip["frames"])] = clip["frames"]

    for role, frames in tracks.items():
        write_wav(OUT / f"delivery-demo-{role}.wav", reference, bytes(frames))
    conversation = mix_pcm16(bytes(tracks["rider"]), bytes(tracks["customer"]))
    write_wav(OUT / "delivery-demo-conversation.wav", reference, conversation)

    manifest = {
        "provider": "Sahara TTS by Intron",
        "sample_rate": reference.framerate,
        "duration_seconds": round(total_seconds, 2),
        "voices": VOICES,
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
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
