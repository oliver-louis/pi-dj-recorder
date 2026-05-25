from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.services.errors import RecorderError
from app.services.models import RecordingFile


DEFAULT_RECORDINGS_DIR = Path("/home/copper/mixes")
SAFE_WAV_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?\.wav$")
TIMESTAMP_IN_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:-\d+)?\.wav$")
UNSAFE_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


class RecordingsStore:
    def __init__(self, recordings_dir: Path = DEFAULT_RECORDINGS_DIR) -> None:
        self.recordings_dir = Path(recordings_dir)

    def ensure_recordings_dir(self) -> None:
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def ensure_waveform_cache_dir(self) -> Path:
        path = self.recordings_dir / ".waveforms"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def recent_recordings(self, limit: int = 50) -> list[RecordingFile]:
        self.ensure_recordings_dir()
        files = [
            path
            for path in self.recordings_dir.glob("*.wav")
            if path.is_file() and self.is_safe_recording_name(path.name)
        ]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [self.recording_file(path) for path in files[:limit]]

    def recording_file(self, path: Path) -> RecordingFile:
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        midi_path = self.midi_log_path_for_name(path.name)
        onair_path = self.onair_log_path_for_name(path.name)
        midi_available = midi_path.is_file()
        onair_available = onair_path.is_file()
        return RecordingFile(
            name=path.name,
            size=stat.st_size,
            modified_time=modified,
            download_url=f"/api/recordings/{path.name}/download",
            play_url=f"/api/recordings/{path.name}/play",
            midi_log_available=midi_available,
            midi_download_url=f"/api/recordings/{path.name}/midi-download" if midi_available else None,
            onair_log_available=onair_available,
            onair_download_url=f"/api/recordings/{path.name}/onair-download" if onair_available else None,
            track_ids_export_url=f"/api/recordings/{path.name}/track-ids-export" if onair_available else None,
        )

    def path_for_recording(self, filename: str) -> Path:
        if not self.is_safe_recording_name(filename):
            raise ValueError("Invalid recording filename.")
        path = self.recordings_dir / filename
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(filename) from exc
        recordings_root = self.recordings_dir.resolve()
        if recordings_root not in resolved.parents:
            raise ValueError("Invalid recording path.")
        if not resolved.is_file():
            raise FileNotFoundError(filename)
        return resolved

    def sidecar_path_for_recording(self, filename: str, suffix: str) -> Path:
        recording_path = self.path_for_recording(filename)
        path = self.recordings_dir / f"{recording_path.name[:-4]}{suffix}"
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(filename) from exc
        recordings_root = self.recordings_dir.resolve()
        if recordings_root not in resolved.parents:
            raise ValueError("Invalid recording path.")
        if not resolved.is_file():
            raise FileNotFoundError(filename)
        return resolved

    def midi_log_path_for_recording(self, filename: str) -> Path:
        return self.sidecar_path_for_recording(filename, ".midi.jsonl")

    def onair_log_path_for_recording(self, filename: str) -> Path:
        return self.sidecar_path_for_recording(filename, ".onair.jsonl")

    def new_filename(self, mix_name: str | None = None, *, default_prefix: str = "mix") -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return self.unique_filename(mix_name, timestamp, default_prefix=default_prefix)

    def unique_filename(
        self,
        mix_name: str | None,
        timestamp: str,
        existing_path: Path | None = None,
        *,
        default_prefix: str = "mix",
    ) -> str:
        slug = self.slugify(mix_name, default_prefix=default_prefix)
        candidate = f"{slug}_{timestamp}.wav"
        candidate_path = self.recordings_dir / candidate
        if not candidate_path.exists() or candidate_path == existing_path:
            return candidate
        for index in range(2, 1000):
            candidate = f"{slug}_{timestamp}-{index}.wav"
            candidate_path = self.recordings_dir / candidate
            if not candidate_path.exists() or candidate_path == existing_path:
                return candidate
        raise RecorderError("Could not generate a unique filename.")

    def rename_recording(self, filename: str, mix_name: str, *, current_path: Path | None = None) -> RecordingFile:
        path = self.path_for_recording(filename)
        if current_path is not None and path == current_path.resolve():
            raise RecorderError("Cannot rename the active recording.")
        old_cache_path = self.waveform_cache_path(path.name)
        old_midi_path = self.midi_log_path_for_name(path.name)
        old_onair_path = self.onair_log_path_for_name(path.name)
        timestamp = self.timestamp_from_filename(path.name)
        target = self.recordings_dir / self.unique_filename(mix_name, timestamp, existing_path=path)
        if target == path:
            return self.recording_file(path)
        path.rename(target)
        old_cache_path.unlink(missing_ok=True)
        if old_midi_path.exists():
            old_midi_path.rename(self.midi_log_path_for_name(target.name))
        if old_onair_path.exists():
            old_onair_path.rename(self.onair_log_path_for_name(target.name))
        return self.recording_file(target)

    def delete_recording(self, filename: str, *, current_path: Path | None = None) -> None:
        path = self.path_for_recording(filename)
        if current_path is not None and path == current_path.resolve():
            raise RecorderError("Cannot delete the active recording.")
        path.unlink()
        self.waveform_cache_path(path.name).unlink(missing_ok=True)
        self.midi_log_path_for_name(path.name).unlink(missing_ok=True)
        self.onair_log_path_for_name(path.name).unlink(missing_ok=True)

    def discard_recording_artifacts(self, filename: str) -> None:
        path = self.recordings_dir / filename
        path.unlink(missing_ok=True)
        self.waveform_cache_path(filename).unlink(missing_ok=True)
        self.midi_log_path_for_name(filename).unlink(missing_ok=True)
        self.onair_log_path_for_name(filename).unlink(missing_ok=True)

    def waveform_cache_path(self, filename: str) -> Path:
        return self.ensure_waveform_cache_dir() / f"{filename}.json"

    def midi_log_path_for_name(self, filename: str) -> Path:
        return self.recordings_dir / f"{filename[:-4]}.midi.jsonl"

    def onair_log_path_for_name(self, filename: str) -> Path:
        return self.recordings_dir / f"{filename[:-4]}.onair.jsonl"

    @staticmethod
    def waveform_signature(stat: object) -> str:
        return f"{stat.st_size}:{int(stat.st_mtime)}"

    @staticmethod
    def read_waveform_cache(path: Path) -> dict[str, object] | None:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def write_waveform_cache(path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload, separators=(",", ":")))

    @staticmethod
    def is_safe_recording_name(filename: str) -> bool:
        return bool(SAFE_WAV_NAME.fullmatch(filename))

    @staticmethod
    def slugify(mix_name: str | None, default_prefix: str = "mix") -> str:
        fallback = UNSAFE_SLUG_CHARS.sub("-", default_prefix.strip().lower()).strip("-_")
        fallback = re.sub(r"-{2,}", "-", fallback)
        fallback = (fallback or "mix")[:80].strip("-_") or "mix"
        if not mix_name:
            return fallback
        slug = UNSAFE_SLUG_CHARS.sub("-", mix_name.strip().lower()).strip("-_")
        slug = re.sub(r"-{2,}", "-", slug)
        return (slug or fallback)[:80].strip("-_") or fallback

    @staticmethod
    def timestamp_from_filename(filename: str) -> str:
        match = TIMESTAMP_IN_NAME.search(filename)
        if match:
            return match.group(1)
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
