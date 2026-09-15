from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .benchmark import evaluate
from .benchmark_providers import (
    ElevenLabsTranscriber,
    IntronFileTranscriber,
    OpenAIWhisperTranscriber,
)
from .config import Settings


async def run_corpus(manifest_path: Path) -> tuple[list[dict], dict]:
    settings = Settings.from_env()
    providers = (
        IntronFileTranscriber(settings),
        OpenAIWhisperTranscriber(settings),
        ElevenLabsTranscriber(settings),
    )
    rows = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    completed: list[dict] = []
    for row in rows:
        if "audio" not in row:
            raise ValueError(f"benchmark row {row.get('id', '<unknown>')} has no audio path")
        audio_path = (manifest_path.parent / row["audio"]).resolve()
        if not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        output = {**row, "hypotheses": {}}
        for provider in providers:
            result = await provider.transcribe(audio_path)
            output["hypotheses"][result.model] = {
                "transcript": result.transcript,
                "latency_ms": result.latency_ms,
            }
        completed.append(output)
    return completed, evaluate(completed)


async def _run(args: argparse.Namespace) -> None:
    rows, report = await run_corpus(args.manifest)
    args.hypotheses_output.parent.mkdir(parents=True, exist_ok=True)
    args.hypotheses_output.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe one labeled corpus with Sahara, Whisper, and ElevenLabs"
    )
    parser.add_argument("manifest", type=Path, help="JSONL with id, audio, reference, labels")
    parser.add_argument(
        "--hypotheses-output",
        type=Path,
        default=Path("artifacts/benchmark-hypotheses.jsonl"),
    )
    parser.add_argument(
        "--report-output", type=Path, default=Path("artifacts/benchmark-report.json")
    )
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
