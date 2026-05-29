from __future__ import annotations

import json
import signal
import subprocess
import threading
import weakref
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from app.services.errors import (
    AlreadyRecordingError,
    DeviceUnavailableError,
    NotMeteringError,
    NotRecordingError,
    RecorderError,
    TrackIdExportError,
    WaveformGenerationError,
)
from app.services.midi_daemon import MISSING, MidiDaemonService
from app.services.midi_logs import MidiLoggingService
from app.services.models import MeterState, RecordingFile, RecordingStatus, WaveformData
from app.services.parsers import AstatsParser, parse_midi_line
from app.services.recording_runtime import RecordingRuntimeService
from app.services.recordings_store import DEFAULT_RECORDINGS_DIR, RecordingsStore
from app.services.settings import AppSettings, SettingsStore
from app.services.track_ids import TrackIdExporter
from app.services.waveforms import WaveformService


def _recorder_watchdog_entry(recorder_ref: "weakref.ReferenceType[Recorder]") -> None:
    while True:
        recorder = recorder_ref()
        if recorder is None:
            return
        if recorder._midi_daemon._daemon_stop_event.wait(1.0):
            return
        resolved_port = recorder._resolve_midi_port()
        with recorder._lock:
            recorder._ensure_midi_daemon_locked(resolved_port)


class Recorder:
    def __init__(
        self,
        recordings_dir: Path = DEFAULT_RECORDINGS_DIR,
        *,
        ffmpeg_bin: str = "ffmpeg",
        input_device: str = "plughw:X2,0",
        midi_capture_bin: str = "aseqdump",
        midi_port: str = "16:0",
        midi_port_name_hint: str = "XONE:96",
        config_path: Path | None = None,
        prolink_status_path: Path | None = None,
        onair_threshold: int = 30,
        stop_timeout_seconds: float = 15.0,
        process_start_grace_seconds: float = 0.35,
        device_check_timeout_seconds: float = 4.0,
        device_check_cache_seconds: float = 5.0,
        device_check_enabled: bool = True,
    ) -> None:
        self.recordings_dir = Path(recordings_dir)
        if config_path is None:
            config_path = Path("config.json") if self.recordings_dir == DEFAULT_RECORDINGS_DIR else self.recordings_dir / "config.json"
        self.config_path = Path(config_path)
        self._settings_store = SettingsStore(
            self.config_path,
            AppSettings(
                midi_port=midi_port,
                midi_port_name_hint=midi_port_name_hint,
                input_device=input_device,
                onair_threshold=onair_threshold,
                prolink_onair_enabled=True,
                prolink_onair_threshold=1,
                prolink_onair_channel_to_player={"2": 2, "3": 3},
                default_mix_prefix="mix",
                track_id_merge_gap_seconds=10.0,
                auto_enable_metering=False,
                theme="dark",
                confirm_delete_recordings=True,
                stop_discard_countdown_seconds=3,
            ),
        )
        loaded_settings = self._settings_store.load()
        self.ffmpeg_bin = ffmpeg_bin
        self.input_device = loaded_settings.input_device
        self.midi_capture_bin = midi_capture_bin
        self.midi_port = loaded_settings.midi_port
        self.midi_port_name_hint = loaded_settings.midi_port_name_hint
        self.onair_threshold = max(0, min(127, int(loaded_settings.onair_threshold)))
        self.prolink_onair_enabled = bool(loaded_settings.prolink_onair_enabled)
        self.prolink_onair_threshold = max(0, min(127, int(loaded_settings.prolink_onair_threshold)))
        try:
            self.prolink_onair_channel_to_player = self._normalize_prolink_mapping(
                loaded_settings.prolink_onair_channel_to_player
            )
        except ValueError:
            self.prolink_onair_channel_to_player = {"2": 2, "3": 3}
        self.default_mix_prefix = loaded_settings.default_mix_prefix
        self.track_id_merge_gap_seconds = loaded_settings.track_id_merge_gap_seconds
        self.auto_enable_metering = loaded_settings.auto_enable_metering
        self.theme = loaded_settings.theme if loaded_settings.theme in {"dark", "light"} else "dark"
        self.confirm_delete_recordings = loaded_settings.confirm_delete_recordings
        self.stop_discard_countdown_seconds = loaded_settings.stop_discard_countdown_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.process_start_grace_seconds = process_start_grace_seconds
        self.device_check_timeout_seconds = device_check_timeout_seconds
        self.device_check_cache_seconds = device_check_cache_seconds
        self.device_check_enabled = device_check_enabled
        self.prolink_status_path = Path(prolink_status_path or "/tmp/pi-prolink-onair-state.json")
        self._lock = threading.RLock()

        self._store = RecordingsStore(self.recordings_dir)
        self._runtime = RecordingRuntimeService(
            ffmpeg_bin=ffmpeg_bin,
            input_device=self.input_device,
            stop_timeout_seconds=stop_timeout_seconds,
            process_start_grace_seconds=process_start_grace_seconds,
            device_check_timeout_seconds=device_check_timeout_seconds,
            device_check_cache_seconds=device_check_cache_seconds,
            device_check_enabled=device_check_enabled,
        )
        self._midi_logs = MidiLoggingService(
            store=self._store,
            midi_capture_bin=midi_capture_bin,
            midi_port=self.midi_port,
            resolve_port=self._resolve_midi_port,
            onair_threshold=self.onair_threshold,
        )
        self._midi_daemon = MidiDaemonService(
            midi_capture_bin=midi_capture_bin,
            midi_port=self.midi_port,
            midi_port_name_hint=self.midi_port_name_hint,
            onair_threshold=self.onair_threshold,
            on_channel_payload=self._handle_daemon_payload,
        )
        self._waveforms = WaveformService(store=self._store, ffmpeg_bin=ffmpeg_bin)
        self._track_ids = TrackIdExporter(store=self._store)

    def ensure_recordings_dir(self) -> None:
        self._store.ensure_recordings_dir()

    def ensure_waveform_cache_dir(self) -> Path:
        return self._store.ensure_waveform_cache_dir()

    def start_midi_daemon(self) -> None:
        self._midi_daemon._daemon_stop_event.clear()
        resolved_port = self._resolve_midi_port()
        with self._lock:
            self._ensure_midi_daemon_locked(resolved_port)
            if (
                self._midi_daemon._daemon_watchdog_thread is None
                or not self._midi_daemon._daemon_watchdog_thread.is_alive()
            ):
                thread = threading.Thread(target=_recorder_watchdog_entry, args=(weakref.ref(self),), daemon=True)
                self._midi_daemon._daemon_watchdog_thread = thread
                thread.start()

    def shutdown(self) -> None:
        with self._lock:
            self._midi_daemon._daemon_stop_event.set()
            self._stop_daemon_midi_locked()

    def midi_state_payload(self) -> dict[str, object]:
        with self._lock:
            self._clear_if_process_exited_locked()
            return self._midi_daemon.state_payload()

    def start(self, mix_name: str | None = None) -> RecordingStatus:
        with self._lock:
            self._clear_if_process_exited_locked()
            if self._runtime.process is not None:
                raise AlreadyRecordingError("A recording is already active.")
            if self._runtime.monitor_process is not None:
                self._runtime.stop_monitor()

            self.ensure_recordings_dir()
            output_path = self.recordings_dir / self._new_filename(mix_name)
            self._runtime.start_recording(output_path)
            self._start_onair_log_locked(output_path)
            self._start_midi_capture_locked(output_path)
            return self._status_locked()

    def stop(self, *, discard: bool = False) -> RecordingStatus:
        with self._lock:
            self._clear_if_process_exited_locked()
            process = self._runtime.process
            if process is None:
                raise NotRecordingError("No recording is active.")
            recording_path = self._runtime.current_path

            self._stop_onair_log_locked()
            self._stop_midi_locked()
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=self.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

            status = self._status_locked(recording_override=False, pid_override=None)
            self._runtime.process = None
            self._runtime._current_path = None
            self._runtime._started_at_monotonic = None
            self._runtime._metering_enabled = False
            self._runtime._meter_state = self._idle_meter_state()
            if discard and recording_path is not None:
                self._store.discard_recording_artifacts(recording_path.name)
            return status

    def status(self) -> RecordingStatus:
        with self._lock:
            self._clear_if_process_exited_locked()
            return self._status_locked()

    def start_metering(self) -> RecordingStatus:
        with self._lock:
            self._clear_if_process_exited_locked()
            if self._runtime.process is not None:
                self._runtime._metering_enabled = True
                return self._status_locked()
            if self._runtime.monitor_process is not None:
                self._runtime._metering_enabled = True
                return self._status_locked()
            command = self._build_metering_command()
            process = self._start_ffmpeg_process(command)
            self._runtime.monitor_process = process
            self._runtime._metering_enabled = True
            self._runtime._meter_state = self._idle_meter_state(recording=False)
            self._start_stderr_reader(process, source="metering")
            return self._status_locked()

    def stop_metering(self) -> RecordingStatus:
        with self._lock:
            self._clear_if_process_exited_locked()
            if self._runtime.process is not None:
                self._runtime._metering_enabled = False
                self._runtime._meter_state = self._idle_meter_state(recording=False)
                return self._status_locked()
            if self._runtime.monitor_process is None:
                raise NotMeteringError("Metering is not active.")
            self._runtime._metering_enabled = False
            self._stop_monitor_locked()
            return self._status_locked()

    def check_device_available(self, *, force: bool = False) -> tuple[bool, str | None]:
        with self._lock:
            self._clear_if_process_exited_locked()
            return self._runtime.check_device_available(midi_online=self._midi_daemon.midi_online, force=force)

    def recent_recordings(self, limit: int = 50) -> list[RecordingFile]:
        return self._store.recent_recordings(limit)

    def rename_recording(self, filename: str, mix_name: str) -> RecordingFile:
        with self._lock:
            current_path = self._runtime.current_path
            return self._store.rename_recording(filename, mix_name, current_path=current_path)

    def delete_recording(self, filename: str) -> None:
        with self._lock:
            current_path = self._runtime.current_path
            self._store.delete_recording(filename, current_path=current_path)

    def meter_state(self) -> MeterState:
        with self._lock:
            self._clear_if_process_exited_locked()
            return self._runtime.meter_state()

    def meter_payload(self) -> dict[str, object]:
        with self._lock:
            self._clear_if_process_exited_locked()
            return self._runtime.meter_payload()

    def waveform_for_recording(self, filename: str) -> WaveformData:
        path = self.path_for_recording(filename)
        stat = path.stat()
        signature = self._waveform_signature(stat)
        duration_seconds = self._wav_duration_seconds(path)
        target_samples = self._target_waveform_samples(duration_seconds)
        cache_path = self._waveform_cache_path(filename)
        cached = self._read_waveform_cache(cache_path)
        if cached is not None and cached.get("signature") == signature and cached.get("target_samples") == target_samples:
            return WaveformData(
                duration_seconds=float(cached["duration_seconds"]),
                sample_count=int(cached["sample_count"]),
                samples=[float(value) for value in cached["samples"]],
                generated_at=str(cached["generated_at"]),
            )

        rms_db_values = self._extract_waveform_rms_db(path)
        if not rms_db_values:
            raise WaveformGenerationError(f"No waveform data could be extracted from {path.name}.")
        samples = self._normalize_waveform(self._downsample_waveform(rms_db_values, target_samples))
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
        self._write_waveform_cache(cache_path, payload)
        return WaveformData(
            duration_seconds=duration_seconds,
            sample_count=len(samples),
            samples=samples,
            generated_at=generated_at,
        )

    def path_for_recording(self, filename: str) -> Path:
        return self._store.path_for_recording(filename)

    def midi_log_path_for_recording(self, filename: str) -> Path:
        return self._store.midi_log_path_for_recording(filename)

    def onair_log_path_for_recording(self, filename: str) -> Path:
        return self._store.onair_log_path_for_recording(filename)

    def track_ids_export_for_recording(self, filename: str) -> tuple[str, bytes]:
        return self._track_ids.export_for_recording(filename, merge_gap_seconds=self.track_id_merge_gap_seconds)

    def settings_payload(self) -> dict[str, object]:
        with self._lock:
            self._clear_if_process_exited_locked()
            midi_devices = self.list_available_midi_devices()
            audio_devices = self.list_available_audio_devices()
            editable, busy_reason = self._settings_editability_locked()
            return {
                "settings": asdict(self._current_settings_locked()),
                "editable": editable,
                "busy_reason": busy_reason,
                "midi_devices": midi_devices,
                "audio_devices": audio_devices,
                "midi_selected_available": any(device["id"] == self.midi_port for device in midi_devices),
                "audio_selected_available": any(device["id"] == self.input_device for device in audio_devices),
                "debug": self._settings_debug_payload_locked(),
            }

    def apply_settings(
        self,
        *,
        midi_port: str,
        input_device: str,
        onair_threshold: int,
        default_mix_prefix: str,
        track_id_merge_gap_seconds: float,
        auto_enable_metering: bool,
        theme: str,
        confirm_delete_recordings: bool,
        stop_discard_countdown_seconds: int,
        prolink_onair_enabled: bool,
        prolink_onair_threshold: int,
        prolink_onair_channel_to_player: dict[str, int],
    ) -> dict[str, object]:
        with self._lock:
            self._clear_if_process_exited_locked()
            editable, _ = self._settings_editability_locked()
            if not editable:
                raise RecorderError("Settings can only be changed while idle.")

            default_mix_prefix = default_mix_prefix.strip()
            if not default_mix_prefix:
                raise ValueError("Default mix prefix cannot be empty.")
            if theme not in {"dark", "light"}:
                raise ValueError("Invalid theme.")
            if not 0 <= int(onair_threshold) <= 127:
                raise ValueError("On-air threshold must be between 0 and 127.")
            if not 0 <= int(prolink_onair_threshold) <= 127:
                raise ValueError("Pro DJ Link on-air threshold must be between 0 and 127.")
            if not 0 <= float(track_id_merge_gap_seconds) <= 30:
                raise ValueError("Track ID merge gap must be between 0 and 30 seconds.")
            if not 0 <= int(stop_discard_countdown_seconds) <= 15:
                raise ValueError("Discard countdown must be between 0 and 15 seconds.")

            midi_devices = self.list_available_midi_devices()
            audio_devices = self.list_available_audio_devices()
            midi_match = next((device for device in midi_devices if device["id"] == midi_port), None)
            if midi_match is None:
                raise ValueError("Invalid MIDI device.")
            audio_match = next((device for device in audio_devices if device["id"] == input_device), None)
            if audio_match is None:
                raise ValueError("Invalid audio device.")

            midi_changed = midi_port != self.midi_port
            audio_changed = input_device != self.input_device

            self.midi_port = midi_port
            self.midi_port_name_hint = str(midi_match["name_hint"])
            self.input_device = input_device
            self.onair_threshold = int(onair_threshold)
            self.prolink_onair_enabled = bool(prolink_onair_enabled)
            self.prolink_onair_threshold = int(prolink_onair_threshold)
            self.prolink_onair_channel_to_player = self._normalize_prolink_mapping(
                prolink_onair_channel_to_player
            )
            self.default_mix_prefix = default_mix_prefix
            self.track_id_merge_gap_seconds = float(track_id_merge_gap_seconds)
            self.auto_enable_metering = bool(auto_enable_metering)
            self.theme = theme
            self.confirm_delete_recordings = bool(confirm_delete_recordings)
            self.stop_discard_countdown_seconds = int(stop_discard_countdown_seconds)
            self._midi_logs.midi_port = midi_port
            self._midi_logs.onair_threshold = self.onair_threshold
            self._midi_daemon.midi_port = midi_port
            self._midi_daemon.midi_port_name_hint = self.midi_port_name_hint
            self._midi_daemon.update_threshold(self.onair_threshold)
            self._runtime.input_device = input_device
            self._runtime._device_check_time = None
            self._runtime._device_available = False
            self._runtime._device_error = None
            self._settings_store.save(
                AppSettings(
                    midi_port=self.midi_port,
                    midi_port_name_hint=self.midi_port_name_hint,
                    input_device=self.input_device,
                    onair_threshold=self.onair_threshold,
                    prolink_onair_enabled=self.prolink_onair_enabled,
                    prolink_onair_threshold=self.prolink_onair_threshold,
                    prolink_onair_channel_to_player=self.prolink_onair_channel_to_player,
                    default_mix_prefix=self.default_mix_prefix,
                    track_id_merge_gap_seconds=self.track_id_merge_gap_seconds,
                    auto_enable_metering=self.auto_enable_metering,
                    theme=self.theme,
                    confirm_delete_recordings=self.confirm_delete_recordings,
                    stop_discard_countdown_seconds=self.stop_discard_countdown_seconds,
                )
            )
            if midi_changed:
                self._stop_daemon_midi_locked()
                self._ensure_midi_daemon_locked()
            elif audio_changed:
                self._runtime._device_check_time = None
            return self.settings_payload()

    def list_available_midi_devices(self) -> list[dict[str, object]]:
        ports = self._list_midi_ports() or []
        return [
            {
                "id": port_id,
                "client_name": client_name,
                "port_name": port_name,
                "name_hint": f"{client_name} {port_name}".strip(),
                "label": f"{port_id} — {client_name} / {port_name}",
            }
            for port_id, client_name, port_name in ports
        ]

    def list_available_audio_devices(self) -> list[dict[str, object]]:
        return self._runtime.list_audio_input_devices()

    @staticmethod
    def is_safe_recording_name(filename: str) -> bool:
        return RecordingsStore.is_safe_recording_name(filename)

    def _new_filename(self, mix_name: str | None = None) -> str:
        return self._store.new_filename(mix_name, default_prefix=self.default_mix_prefix)

    def _unique_filename(self, mix_name: str | None, timestamp: str, existing_path: Path | None = None) -> str:
        return self._store.unique_filename(
            mix_name,
            timestamp,
            existing_path,
            default_prefix=self.default_mix_prefix,
        )

    def _slugify(self, mix_name: str | None) -> str:
        return RecordingsStore.slugify(mix_name, default_prefix=self.default_mix_prefix)

    @staticmethod
    def _timestamp_from_filename(filename: str) -> str:
        return RecordingsStore.timestamp_from_filename(filename)

    def _build_ffmpeg_command(self, output_path: Path) -> list[str]:
        return self._runtime.build_ffmpeg_command(output_path)

    def _build_device_check_command(self) -> list[str]:
        return self._runtime.build_device_check_command()

    def _build_metering_command(self) -> list[str]:
        return self._runtime.build_metering_command()

    def _clear_if_process_exited_locked(self) -> None:
        self._runtime.clear_if_process_exited(self._stop_midi_locked)
        self._midi_logs.clear_if_capture_exited()
        if self._runtime.process is None and self._midi_logs._onair_log_path is not None:
            self._stop_onair_log_locked()
        self._midi_daemon.clear_if_process_exited()

    def _status_locked(
        self,
        *,
        recording_override: bool | None = None,
        pid_override: int | None = None,
    ) -> RecordingStatus:
        device_available, device_error = self._runtime.check_device_available(
            midi_online=self._midi_daemon.midi_online,
            force=False,
        )
        return self._runtime.status(
            recording_override=recording_override,
            pid_override=pid_override,
            device_available=device_available,
            device_error=device_error,
        )

    def _settings_editability_locked(self) -> tuple[bool, str | None]:
        if self._runtime.process is not None:
            return False, "recording"
        if self._runtime.monitor_process is not None:
            return False, "metering"
        return True, None

    def _current_settings_locked(self) -> AppSettings:
        return AppSettings(
            midi_port=self.midi_port,
            midi_port_name_hint=self.midi_port_name_hint,
            input_device=self.input_device,
            onair_threshold=self.onair_threshold,
            prolink_onair_enabled=self.prolink_onair_enabled,
            prolink_onair_threshold=self.prolink_onair_threshold,
            prolink_onair_channel_to_player=self.prolink_onair_channel_to_player,
            default_mix_prefix=self.default_mix_prefix,
            track_id_merge_gap_seconds=self.track_id_merge_gap_seconds,
            auto_enable_metering=self.auto_enable_metering,
            theme=self.theme,
            confirm_delete_recordings=self.confirm_delete_recordings,
            stop_discard_countdown_seconds=self.stop_discard_countdown_seconds,
        )

    def _settings_debug_payload_locked(self) -> dict[str, object]:
        device_available, device_error = self._runtime.check_device_available(
            midi_online=self._midi_daemon.midi_online,
            force=False,
        )
        return {
            "selected_midi_device": self.midi_port,
            "resolved_midi_port": self._daemon_port_in_use,
            "onair_threshold": self.onair_threshold,
            "selected_audio_input": self.input_device,
            "midi_online": self._midi_daemon.midi_online,
            "midi_error": self._midi_daemon._midi_error,
            "device_available": device_available,
            "device_error": device_error,
            "recording": self._runtime.process is not None,
            "metering_active": self._runtime._metering_enabled,
            "config_path": str(self.config_path),
            "prolink_status_path": str(self.prolink_status_path),
            "prolink_onair": self._prolink_status_payload(),
        }

    @staticmethod
    def _normalize_prolink_mapping(mapping: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for channel in ("2", "3"):
            value = mapping.get(channel)
            try:
                player = int(value)
            except (TypeError, ValueError):
                raise ValueError("Pro DJ Link player mappings must be numbers.") from None
            if not 1 <= player <= 6:
                raise ValueError("Pro DJ Link player mappings must be between 1 and 6.")
            normalized[channel] = player
        return normalized

    def _prolink_status_payload(self) -> dict[str, object]:
        try:
            raw = json.loads(self.prolink_status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"available": False, "error": "Status file not found."}
        except (json.JSONDecodeError, OSError) as exc:
            return {"available": False, "error": str(exc)}
        if not isinstance(raw, dict):
            return {"available": False, "error": "Status file did not contain an object."}
        raw["available"] = True
        return raw

    def _file_size(self, path: Path | None) -> int:
        return self._runtime.file_size(path)

    def _recording_file(self, path: Path) -> RecordingFile:
        return self._store.recording_file(path)

    def _clean_error(self, stderr: str | None) -> str:
        return self._runtime.clean_error(stderr)

    def _waveform_cache_path(self, filename: str) -> Path:
        return self._store.waveform_cache_path(filename)

    def _midi_log_path_for_recording(self, filename: str) -> Path:
        return self._store.midi_log_path_for_name(filename)

    def _onair_log_path_for_recording(self, filename: str) -> Path:
        return self._store.onair_log_path_for_name(filename)

    @staticmethod
    def _track_title_for_channel(channel: int) -> str:
        return TrackIdExporter.track_title_for_channel(channel)

    @staticmethod
    def _format_track_time(seconds: float) -> str:
        return TrackIdExporter.format_track_time(seconds)

    def _track_sessions_from_onair_events(self, events: list[dict[str, object]]) -> list[dict[str, float | int]]:
        return self._track_ids.track_sessions_from_onair_events(
            events,
            merge_gap_seconds=self.track_id_merge_gap_seconds,
        )

    @staticmethod
    def _event_time_seconds(event: dict[str, object]) -> float:
        return TrackIdExporter.event_time_seconds(event)

    @staticmethod
    def _waveform_signature(stat: Any) -> str:
        return RecordingsStore.waveform_signature(stat)

    def _read_waveform_cache(self, path: Path) -> dict[str, Any] | None:
        cached = self._store.read_waveform_cache(path)
        if cached is None:
            return None
        return dict(cached)

    def _write_waveform_cache(self, path: Path, payload: dict[str, Any]) -> None:
        self._store.write_waveform_cache(path, payload)

    def _wav_duration_seconds(self, path: Path) -> float:
        return self._waveforms.wav_duration_seconds(path)

    @staticmethod
    def _target_waveform_samples(duration_seconds: float) -> int:
        return WaveformService.target_waveform_samples(duration_seconds)

    def _build_waveform_command(self, path: Path) -> list[str]:
        return self._waveforms.build_waveform_command(path)

    def _extract_waveform_rms_db(self, path: Path) -> list[float]:
        return self._waveforms.extract_waveform_rms_db(path)

    @staticmethod
    def _db_to_linear(value: str) -> float:
        return WaveformService.db_to_linear(value)

    @staticmethod
    def _downsample_waveform(values: list[float], bins: int) -> list[float]:
        return WaveformService.downsample_waveform(values, bins)

    @staticmethod
    def _normalize_waveform(values: list[float]) -> list[float]:
        return WaveformService.normalize_waveform(values)

    def _start_stderr_reader(self, process: subprocess.Popen[Any], *, source: str) -> None:
        self._runtime.start_stderr_reader(process, source=source)

    def _start_ffmpeg_process(self, command: list[str]) -> subprocess.Popen[Any]:
        return self._runtime.start_ffmpeg_process(command)

    def _read_ffmpeg_stderr(self, process: subprocess.Popen[Any], source: str) -> None:
        self._runtime.read_ffmpeg_stderr(process, source)

    def _stop_monitor_locked(self) -> None:
        self._runtime.stop_monitor()

    def _start_midi_capture_locked(self, recording_path: Path) -> None:
        self._midi_logs.start_capture(recording_path)

    def _stop_midi_locked(self) -> None:
        self._midi_logs.stop_capture()

    def _start_onair_log_locked(self, recording_path: Path) -> None:
        self._midi_logs.start_onair_log(recording_path, self._midi_daemon.channels())

    def _stop_onair_log_locked(self) -> None:
        self._midi_logs.stop_onair_log(self._recording_elapsed_seconds())

    def _write_onair_event_locked(self, payload: dict[str, object]) -> None:
        self._midi_logs.write_onair_event(payload)

    def _recording_elapsed_seconds(self) -> float:
        return self._runtime.recording_elapsed_seconds()

    def _build_midi_command(self, resolved_port: str | None = None) -> list[str]:
        return self._midi_logs.build_midi_command(resolved_port)

    def _resolve_midi_port(self) -> str | None:
        return self._midi_daemon.resolve_midi_port()

    def _list_midi_ports(self) -> list[tuple[str, str, str]] | None:
        return self._midi_daemon.list_midi_ports()

    def _ensure_midi_daemon_locked(self, resolved_port: str | None | object = MISSING) -> None:
        if resolved_port is MISSING:
            resolved_port = self._resolve_midi_port()
        process = self._daemon_midi_process
        if process is not None and process.poll() is None:
            if resolved_port is None:
                self._stop_daemon_midi_locked()
            elif self._daemon_port_in_use is not None and resolved_port != self._daemon_port_in_use:
                self._stop_daemon_midi_locked()
            else:
                self._midi_daemon._midi_online = True
                self._midi_daemon._midi_error = None
                return
        if self._midi_daemon._daemon_stop_event.is_set():
            return
        try:
            command = self._build_midi_command(resolved_port if isinstance(resolved_port, str) else None)
        except FileNotFoundError as exc:
            self._daemon_midi_process = None
            self._daemon_port_in_use = None
            self._midi_daemon._midi_online = False
            self._midi_daemon._midi_error = str(exc)
            return
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            self._daemon_midi_process = None
            self._midi_daemon._midi_online = False
            self._midi_daemon._midi_error = str(exc)
            return
        self._daemon_midi_process = process
        self._daemon_port_in_use = command[-1]
        self._midi_daemon._midi_online = True
        self._midi_daemon._midi_error = None
        thread = threading.Thread(target=self._read_daemon_midi_stream, args=(process,), daemon=True)
        self._midi_daemon._daemon_midi_reader_thread = thread
        thread.start()

    def _stop_daemon_midi_locked(self) -> None:
        self._midi_daemon.stop_locked()

    def _midi_daemon_watchdog_loop(self) -> None:
        _recorder_watchdog_entry(weakref.ref(self))

    def _read_daemon_midi_stream(self, process: subprocess.Popen[Any]) -> None:
        self._midi_daemon._read_stream(process)

    def _apply_daemon_midi_payload_locked(self, payload: dict[str, object]) -> None:
        current_state, next_on_air = self._midi_daemon.apply_payload_locked(payload)
        if current_state is None or next_on_air is None:
            return
        self._midi_logs.apply_daemon_payload(
            payload,
            current_state=current_state,
            next_on_air=next_on_air,
            elapsed_seconds=self._recording_elapsed_seconds(),
        )

    def _handle_daemon_payload(self, payload: dict[str, object]) -> None:
        with self._lock:
            self._clear_if_process_exited_locked()
            self._apply_daemon_midi_payload_locked(payload)

    def _read_midi_stream(self, process: subprocess.Popen[Any], log_path: Path) -> None:
        self._midi_logs._read_midi_stream(process, log_path)

    def _parse_midi_line(self, line: str) -> dict[str, object]:
        return parse_midi_line(line, source_port=self.midi_port, elapsed_ms=self._midi_elapsed_ms())

    def _midi_elapsed_ms(self) -> int:
        return self._midi_logs.midi_elapsed_ms()

    @staticmethod
    def _idle_meter_state(recording: bool = False) -> MeterState:
        return RecordingRuntimeService.idle_meter_state(recording)

    @property
    def _daemon_midi_process(self) -> subprocess.Popen[Any] | None:
        return self._midi_daemon.daemon_process

    @_daemon_midi_process.setter
    def _daemon_midi_process(self, value: subprocess.Popen[Any] | None) -> None:
        self._midi_daemon.daemon_process = value

    @property
    def _daemon_port_in_use(self) -> str | None:
        return self._midi_daemon.daemon_port_in_use

    @_daemon_port_in_use.setter
    def _daemon_port_in_use(self, value: str | None) -> None:
        self._midi_daemon.daemon_port_in_use = value

    @property
    def _midi_online(self) -> bool:
        return self._midi_daemon.midi_online

    @_midi_online.setter
    def _midi_online(self, value: bool) -> None:
        self._midi_daemon.midi_online = value


__all__ = [
    "AstatsParser",
    "AlreadyRecordingError",
    "DeviceUnavailableError",
    "NotMeteringError",
    "NotRecordingError",
    "Recorder",
    "RecorderError",
    "TrackIdExportError",
    "WaveformGenerationError",
]
