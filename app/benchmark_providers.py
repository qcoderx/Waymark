from __future__ import annotations

import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings


@dataclass(slots=True)
class TranscriptionResult:
    model: str
    transcript: str
    latency_ms: float


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class IntronFileTranscriber:
    name = "sahara"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def transcribe(self, audio_path: Path) -> TranscriptionResult:
        if not self.settings.intron_api_key:
            raise RuntimeError("INTRON_API_KEY is required")
        started = time.perf_counter()
        headers = {"Authorization": f"Bearer {self.settings.intron_api_key}"}
        data = {
            "audio_file_name": audio_path.name,
            "use_language_asr_input": self.settings.intron_language,
            "use_category": "file_category_general",
            "use_disable_llm_corrections": "TRUE",
        }
        files = {
            "audio_file_blob": (
                audio_path.name,
                audio_path.read_bytes(),
                _mime_type(audio_path),
            )
        }
        async with httpx.AsyncClient(timeout=130) as client:
            response = await client.post(
                self.settings.intron_file_url, headers=headers, data=data, files=files
            )
            response.raise_for_status()
            payload = response.json()
        transcript = payload.get("data", {}).get("audio_transcript", "")
        if not transcript:
            raise RuntimeError(f"Sahara returned no transcript: {payload}")
        return TranscriptionResult(
            model=self.name,
            transcript=transcript,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


class OpenAIWhisperTranscriber:
    name = "whisper"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def transcribe(self, audio_path: Path) -> TranscriptionResult:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        started = time.perf_counter()
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        data = {
            "model": self.settings.openai_transcription_model,
            "response_format": "json",
        }
        files = {"file": (audio_path.name, audio_path.read_bytes(), _mime_type(audio_path))}
        async with httpx.AsyncClient(timeout=130) as client:
            response = await client.post(
                f"{self.settings.openai_base_url}/audio/transcriptions",
                headers=headers,
                data=data,
                files=files,
            )
            response.raise_for_status()
            payload = response.json()
        transcript = payload.get("text", "")
        if not transcript:
            raise RuntimeError(f"OpenAI Whisper returned no transcript: {payload}")
        return TranscriptionResult(
            model=self.name,
            transcript=transcript,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )


class ElevenLabsTranscriber:
    name = "elevenlabs"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def transcribe(self, audio_path: Path) -> TranscriptionResult:
        if not self.settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is required")
        started = time.perf_counter()
        headers = {"xi-api-key": self.settings.elevenlabs_api_key}
        params = {
            "model_id": self.settings.elevenlabs_stt_model,
        }
        files = {"file": (audio_path.name, audio_path.read_bytes(), _mime_type(audio_path))}
        async with httpx.AsyncClient(timeout=130) as client:
            response = await client.post(
                f"{self.settings.elevenlabs_base_url}/speech-to-text",
                headers=headers,
                data=params,
                files=files,
            )
            response.raise_for_status()
            payload = response.json()
        transcript = payload.get("text", "")
        if not transcript:
            raise RuntimeError(f"ElevenLabs returned no transcript: {payload}")
        return TranscriptionResult(
            model=self.name,
            transcript=transcript,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
