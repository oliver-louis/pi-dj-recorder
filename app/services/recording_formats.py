from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class RecordingFormatSpec:
    id: str
    extension: str
    label: str
    media_type: str
    encoder: str
    ffmpeg_args: tuple[str, ...]


RECORDING_FORMATS: dict[str, RecordingFormatSpec] = {
    "wav": RecordingFormatSpec(
        id="wav",
        extension=".wav",
        label="WAV — 24-bit lossless",
        media_type="audio/wav",
        encoder="pcm_s24le",
        ffmpeg_args=("-c:a", "pcm_s24le"),
    ),
    "flac": RecordingFormatSpec(
        id="flac",
        extension=".flac",
        label="FLAC — 24-bit lossless",
        media_type="audio/flac",
        encoder="flac",
        ffmpeg_args=(
            "-c:a",
            "flac",
            "-sample_fmt",
            "s32",
            "-bits_per_raw_sample",
            "24",
            "-compression_level",
            "5",
        ),
    ),
    "mp3": RecordingFormatSpec(
        id="mp3",
        extension=".mp3",
        label="MP3 — 320 kbps",
        media_type="audio/mpeg",
        encoder="libmp3lame",
        ffmpeg_args=("-c:a", "libmp3lame", "-b:a", "320k"),
    ),
}

ENCODER_LINE = re.compile(r"^A[.A-Z]{5}\s+(\S+)")


def get_recording_format(format_id: str) -> RecordingFormatSpec:
    try:
        return RECORDING_FORMATS[format_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported recording format: {format_id}.") from exc


def recording_format_for_filename(filename: str | Path) -> RecordingFormatSpec:
    suffix = Path(filename).suffix.lower()
    for spec in RECORDING_FORMATS.values():
        if spec.extension == suffix:
            return spec
    raise ValueError(f"Unsupported recording extension: {suffix or '(none)'}.")


def normalize_recording_format(value: object, default: str = "wav") -> str:
    format_id = str(value or "").lower()
    return format_id if format_id in RECORDING_FORMATS else default


@lru_cache(maxsize=None)
def probe_ffmpeg_encoders(ffmpeg_bin: str) -> tuple[frozenset[str], str | None]:
    command = [ffmpeg_bin, "-hide_banner", "-encoders"]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return frozenset(), f"{ffmpeg_bin} was not found."
    except OSError as exc:
        return frozenset(), f"Could not run {ffmpeg_bin}: {exc}."
    except subprocess.TimeoutExpired:
        return frozenset(), "Timed out checking FFmpeg recording formats."
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Could not inspect FFmpeg encoders.").strip()
        return frozenset(), detail[-300:]

    encoders: set[str] = set()
    for line in (result.stdout or "").splitlines():
        match = ENCODER_LINE.match(line.strip())
        if match:
            encoders.add(match.group(1))
    return frozenset(encoders), None


def recording_format_capabilities(ffmpeg_bin: str) -> list[dict[str, object]]:
    encoders, probe_error = probe_ffmpeg_encoders(ffmpeg_bin)
    capabilities: list[dict[str, object]] = []
    for spec in RECORDING_FORMATS.values():
        available = spec.encoder in encoders
        reason = None
        if not available:
            reason = probe_error or f"FFmpeg encoder '{spec.encoder}' is not available."
        capabilities.append(
            {
                "id": spec.id,
                "label": spec.label,
                "available": available,
                "reason": reason,
            }
        )
    return capabilities


# The deployed app uses the default binary, so populate its process-wide cache
# during import before request handling begins.
probe_ffmpeg_encoders("ffmpeg")
