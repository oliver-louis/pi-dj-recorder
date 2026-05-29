from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordingStatus:
    recording: bool
    metering_active: bool
    current_filename: str | None
    pid: int | None
    elapsed_seconds: int
    current_file_size: int
    device_available: bool
    device_error: str | None
    prolink_metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class RecordingFile:
    name: str
    size: int
    modified_time: str
    download_url: str
    play_url: str
    midi_log_available: bool = False
    midi_download_url: str | None = None
    onair_log_available: bool = False
    onair_download_url: str | None = None
    track_ids_export_url: str | None = None


@dataclass(frozen=True)
class MidiChannelState:
    channel_name: str
    controller_id: int
    value: int
    on_air: bool
    last_changed_at: str | None


@dataclass(frozen=True)
class MeterChannel:
    peak_db: float | None
    rms_db: float | None


@dataclass(frozen=True)
class MeterState:
    recording: bool
    channels: dict[str, MeterChannel]
    updated_at: str | None


@dataclass(frozen=True)
class WaveformData:
    duration_seconds: float
    sample_count: int
    samples: list[float]
    generated_at: str
