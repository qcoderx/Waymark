from __future__ import annotations

import base64
import io
import json
import struct
import wave
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets

from .config import Settings


def pcm16_has_speech(payload: bytes, rms_threshold: int = 180) -> bool:
    """Reject silent microphone windows before opening a metered STT session."""

    usable = len(payload) - len(payload) % 2
    if usable < 2:
        return False
    samples = struct.unpack(f"<{usable // 2}h", payload[:usable])
    stride = max(1, len(samples) // 8_000)
    sampled = samples[::stride]
    mean_square = sum(sample * sample for sample in sampled) / len(sampled)
    return mean_square >= rms_threshold * rms_threshold


def mulaw_to_pcm16_16khz(payload: bytes) -> bytes:
    """Decode Twilio's mono 8 kHz μ-law into mono PCM16 at 16 kHz."""

    samples: list[int] = []
    for byte in payload:
        value = (~byte) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        samples.append(-sample if sign else sample)
    if not samples:
        return b""
    upsampled: list[int] = []
    previous = samples[0]
    for sample in samples:
        upsampled.append(previous)
        upsampled.append((previous + sample) // 2)
        previous = sample
    return struct.pack(f"<{len(upsampled)}h", *upsampled)


def resample_pcm16(payload: bytes, source_rate: int, target_rate: int = 16_000) -> bytes:
    """Resample little-endian mono PCM16 using windowed averaging."""

    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    usable = len(payload) - len(payload) % 2
    if not usable:
        return b""
    if source_rate == target_rate:
        return payload[:usable]
    samples = struct.unpack(f"<{usable // 2}h", payload[:usable])
    output_count = max(1, round(len(samples) * target_rate / source_rate))
    ratio = source_rate / target_rate
    output: list[int] = []
    for index in range(output_count):
        start = min(len(samples) - 1, int(index * ratio))
        end = min(len(samples), max(start + 1, int((index + 1) * ratio)))
        window = samples[start:end]
        output.append(round(sum(window) / len(window)))
    return struct.pack(f"<{len(output)}h", *output)


class SaharaStream:
    MIN_PCM_CHUNK_BYTES = 2048
    MAX_PCM_CHUNK_BYTES = 32 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._socket = None
        self._buffer = bytearray()
        self._ack_id = 0

    async def connect(self) -> None:
        if not self.settings.intron_api_key:
            raise RuntimeError("INTRON_API_KEY is required for live Sahara transcription")
        query = urlencode(
            {
                "sample_rate": self.settings.intron_sample_rate,
                "bit_rate": 16,
                "num_channels": 1,
                "use_language_asr_input": self.settings.intron_language,
            }
        )
        self._socket = await websockets.connect(
            f"{self.settings.intron_stream_url}?{query}",
            additional_headers={"Authorization": f"Bearer {self.settings.intron_api_key}"},
            compression=None,
            open_timeout=8,
            close_timeout=3,
            max_size=2**20,
        )

    async def send_twilio_media(self, encoded_mulaw: str) -> None:
        raw = base64.b64decode(encoded_mulaw, validate=True)
        await self.send_pcm16(mulaw_to_pcm16_16khz(raw))

    async def send_pcm16(self, audio: bytes, source_rate: int = 16_000) -> None:
        self._buffer.extend(resample_pcm16(audio, source_rate))
        while len(self._buffer) >= self.MIN_PCM_CHUNK_BYTES:
            size = min(len(self._buffer), self.MAX_PCM_CHUNK_BYTES)
            size -= size % 2
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            await self._send_chunk(chunk)

    async def _send_chunk(self, chunk: bytes) -> None:
        if self._socket is None:
            raise RuntimeError("Sahara stream is not connected")
        self._ack_id += 1
        await self._socket.send(
            json.dumps(
                {
                    "message_type": "INPUT_AUDIO_CHUNK",
                    "audio_base_64": base64.b64encode(chunk).decode(),
                    "ack_id": self._ack_id,
                }
            )
        )

    async def commit(self) -> None:
        if self._socket is None:
            return
        if self._buffer:
            padding = self.MIN_PCM_CHUNK_BYTES - len(self._buffer)
            if padding > 0:
                self._buffer.extend(b"\x00" * padding)
            await self._send_chunk(bytes(self._buffer))
            self._buffer.clear()
        await self._socket.send(json.dumps({"message_type": "COMMIT"}))

    async def messages(self) -> AsyncIterator[dict]:
        if self._socket is None:
            raise RuntimeError("Sahara stream is not connected")
        async for raw in self._socket:
            yield json.loads(raw)

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()


async def transcribe_pcm16_file_segment(settings: Settings, payload: bytes) -> str | None:
    """Transcribe one speech window through Sahara's concurrency-safe file API."""

    if not settings.intron_api_key or not pcm16_has_speech(payload):
        return None
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(payload[: len(payload) - len(payload) % 2])
    headers = {"Authorization": f"Bearer {settings.intron_api_key}"}
    data = {
        "audio_file_name": "waymark-live-window.wav",
        "use_language_asr_input": settings.intron_language,
        "use_category": "file_category_general",
        "use_disable_llm_corrections": "TRUE",
    }
    files = {
        "audio_file_blob": (
            "waymark-live-window.wav",
            audio.getvalue(),
            "audio/wav",
        )
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            settings.intron_file_url,
            headers=headers,
            data=data,
            files=files,
        )
        response.raise_for_status()
        result = response.json()
    transcript = result.get("data", {}).get("audio_transcript", "").strip()
    return transcript or None
