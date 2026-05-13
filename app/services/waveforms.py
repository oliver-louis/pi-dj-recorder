from __future__ import annotations

import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path

from app.services.errors import WaveformGenerationError
from app.services.models import WaveformData
from app.services.parsers import WAVEFORM_RMS_METADATA
from app.services.recordings_store import RecordingsStore


class WaveformService:
    def __init__(self, ffmpeg_bin: str, store: RecordingsStore) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.store = store

    def waveform_for_recording(self, filename: str) -> WaveformData:
        path = self.store.path_for_recording(filename)
        stat = path.stat()
        signature = self.store.waveform_signature(stat)
        target_samples = self.target_waveform_samples(self.wav_duration_seconds(path))
        cache_path = self.store.waveform_cache_path(filename)
        cached = self.store.read_waveform_cache(cache_path)
        if cached is not None and cached.get("signature") == signature and cached.get("target_samples") == target_samples:
            return WaveformData(
                duration_seconds=float(cached["duration_seconds"]),
                sample_count=int(cached["sample_count"]),
                samples=[float(value) for value in cached["samples"]],
                generated_at=str(cached["generated_at"]),
            )

        duration_seconds = self.wav_duration_seconds(path)
        rms_db_values = self.extract_waveform_rms_db(path)
        if not rms_db_values:
            raise WaveformGenerationError(f"No waveform data could be extracted from {path.name}.")
        samples = self.normalize_waveform(self.downsample_waveform(rms_db_values, target_samples))
        if not samples:
            raise WaveformGenerationError(f"No waveform samples could be generated from {path.name}.")
        generated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "signature": signature,
            "target_samples": target_samples,
            "duration_seconds": duration_seconds,
            "sample_count": len(samples),
            "samples": samples,
            "generated_at": generated_at,
        }
        self.store.write_waveform_cache(cache_path, payload)
        return WaveformData(duration_seconds=duration_seconds, sample_count=len(samples), samples=samples, generated_at=generated_at)

    def build_waveform_command(self, path: Path) -> list[str]:
        return [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-ac",
            "1",
            "-filter:a",
            ("astats=metadata=1:reset=1," "ametadata=mode=print:key=lavfi.astats.1.RMS_level"),
            "-f",
            "null",
            "-",
        ]

    def wav_duration_seconds(self, path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as handle:
                frame_rate = handle.getframerate()
                if frame_rate <= 0:
                    return 0.0
                return handle.getnframes() / frame_rate
        except (wave.Error, OSError) as exc:
            raise WaveformGenerationError(f"Could not read WAV duration for {path.name}.") from exc

    def extract_waveform_rms_db(self, path: Path) -> list[float]:
        command = self.build_waveform_command(path)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if process.stderr is None:
            raise WaveformGenerationError("Waveform extraction process did not produce stderr output.")
        values: list[float] = []
        for line in process.stderr:
            match = WAVEFORM_RMS_METADATA.search(line)
            if not match:
                continue
            values.append(self.db_to_linear(match.group(1)))
        return_code = process.wait()
        if return_code != 0:
            raise WaveformGenerationError(f"Waveform extraction failed for {path.name}.")
        if not values:
            raise WaveformGenerationError(f"No waveform data could be extracted from {path.name}.")
        return values

    @staticmethod
    def target_waveform_samples(duration_seconds: float) -> int:
        if duration_seconds <= 0:
            return 600
        return max(600, min(2000, int(duration_seconds / 5)))

    @staticmethod
    def db_to_linear(value: str) -> float:
        lowered = value.lower()
        if lowered in {"-inf", "inf", "+inf"}:
            return 0.0
        try:
            db = float(value)
        except ValueError:
            return 0.0
        return 10 ** (db / 20)

    @staticmethod
    def downsample_waveform(values: list[float], bins: int) -> list[float]:
        if not values:
            return []
        bins = max(1, min(bins, len(values)))
        step = len(values) / bins
        sampled: list[float] = []
        for index in range(bins):
            start = int(index * step)
            end = int((index + 1) * step)
            chunk = values[start:max(start + 1, end)]
            sampled.append(sum(chunk) / len(chunk))
        return sampled

    @staticmethod
    def normalize_waveform(values: list[float]) -> list[float]:
        if not values:
            return []
        peak = max(values)
        if peak <= 0:
            return [0.0 for _ in values]
        return [max(0.0, min(1.0, value / peak)) for value in values]
