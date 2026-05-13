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
        onair_threshold: int = 30,
        stop_timeout_seconds: float = 15.0,
        process_start_grace_seconds: float = 0.35,
        device_check_timeout_seconds: float = 4.0,
        device_check_cache_seconds: float = 5.0,
        device_check_enabled: bool = True,
    ) -> None:
        self.recordings_dir = Path(recordings_dir)
        self.ffmpeg_bin = ffmpeg_bin
        self.input_device = input_device
        self.midi_capture_bin = midi_capture_bin
        self.midi_port = midi_port
        self.midi_port_name_hint = midi_port_name_hint
        self.onair_threshold = max(0, min(127, int(onair_threshold)))
        self.stop_timeout_seconds = stop_timeout_seconds
        self.process_start_grace_seconds = process_start_grace_seconds
        self.device_check_timeout_seconds = device_check_timeout_seconds
        self.device_check_cache_seconds = device_check_cache_seconds
        self.device_check_enabled = device_check_enabled
        self._lock = threading.RLock()

        self._store = RecordingsStore(self.recordings_dir)
        self._runtime = RecordingRuntimeService(
            ffmpeg_bin=ffmpeg_bin,
            input_device=input_device,
            stop_timeout_seconds=stop_timeout_seconds,
            process_start_grace_seconds=process_start_grace_seconds,
            device_check_timeout_seconds=device_check_timeout_seconds,
            device_check_cache_seconds=device_check_cache_seconds,
            device_check_enabled=device_check_enabled,
        )
        self._midi_logs = MidiLoggingService(
            store=self._store,
            midi_capture_bin=midi_capture_bin,
            midi_port=midi_port,
            resolve_port=self._resolve_midi_port,
            onair_threshold=self.onair_threshold,
        )
        self._midi_daemon = MidiDaemonService(
            midi_capture_bin=midi_capture_bin,
            midi_port=midi_port,
            midi_port_name_hint=midi_port_name_hint,
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
        return self._track_ids.export_for_recording(filename)

    @staticmethod
    def is_safe_recording_name(filename: str) -> bool:
        return RecordingsStore.is_safe_recording_name(filename)

    def _new_filename(self, mix_name: str | None = None) -> str:
        return self._store.new_filename(mix_name)

    def _unique_filename(self, mix_name: str | None, timestamp: str, existing_path: Path | None = None) -> str:
        return self._store.unique_filename(mix_name, timestamp, existing_path)

    @staticmethod
    def _slugify(mix_name: str | None) -> str:
        return RecordingsStore.slugify(mix_name)

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
        return self._track_ids.track_sessions_from_onair_events(events)

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
