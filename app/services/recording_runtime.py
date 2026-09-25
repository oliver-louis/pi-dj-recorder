from __future__ import annotations

import logging
import re
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable

from app.services.errors import DeviceUnavailableError, NotMeteringError
from app.services.models import MeterState, RecordingStatus
from app.services.parsers import AstatsParser
from app.services.recording_formats import recording_format_for_filename


logger = logging.getLogger(__name__)


class RecordingRuntimeService:
    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        input_device: str,
        stop_timeout_seconds: float,
        process_start_grace_seconds: float,
        recording_ready_timeout_seconds: float,
        device_check_timeout_seconds: float,
        device_check_cache_seconds: float,
        device_check_enabled: bool,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.input_device = input_device
        self.stop_timeout_seconds = stop_timeout_seconds
        self.process_start_grace_seconds = process_start_grace_seconds
        self.recording_ready_timeout_seconds = recording_ready_timeout_seconds
        self.device_check_timeout_seconds = device_check_timeout_seconds
        self.device_check_cache_seconds = device_check_cache_seconds
        self.device_check_enabled = device_check_enabled
        self._process: subprocess.Popen[Any] | None = None
        self._monitor_process: subprocess.Popen[Any] | None = None
        self._current_path: Path | None = None
        self._started_at_monotonic: float | None = None
        self._started_at_utc: datetime | None = None
        self._audio_progress_seconds = 0.0
        self._audio_progress_observed_at: float | None = None
        self._recording_ready_event = threading.Event()
        self._progress_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._last_ffmpeg_error: str | None = None
        self._metering_enabled = False
        self._meter_state = self.idle_meter_state()
        self._device_check_time: float | None = None
        self._device_available = False
        self._device_error: str | None = None

    @property
    def process(self) -> subprocess.Popen[Any] | None:
        return self._process

    @process.setter
    def process(self, value: subprocess.Popen[Any] | None) -> None:
        self._process = value

    @property
    def monitor_process(self) -> subprocess.Popen[Any] | None:
        return self._monitor_process

    @monitor_process.setter
    def monitor_process(self, value: subprocess.Popen[Any] | None) -> None:
        self._monitor_process = value

    @property
    def current_path(self) -> Path | None:
        return self._current_path

    @property
    def metering_enabled(self) -> bool:
        return self._metering_enabled

    def start_recording(self, output_path: Path, *, ready_deadline: float | None = None) -> None:
        startup_started_at = monotonic()
        command = self.build_ffmpeg_command(output_path)
        process = self.start_ffmpeg_process(command, capture_stdout=True, wait_for_grace=False)
        self._process = process
        self._current_path = output_path
        self._started_at_monotonic = None
        self._started_at_utc = None
        self._audio_progress_seconds = 0.0
        self._audio_progress_observed_at = None
        self._recording_ready_event.clear()
        self._last_ffmpeg_error = None
        self._metering_enabled = True
        self._meter_state = self.idle_meter_state(recording=True)
        self.start_stderr_reader(process, source="recording")
        self.start_progress_reader(process)
        logger.info("Recording FFmpeg launched pid=%s path=%s", process.pid, output_path)

        deadline = ready_deadline or startup_started_at + self.recording_ready_timeout_seconds
        while monotonic() < deadline:
            if process.poll() is not None:
                if self._stderr_thread is not None:
                    self._stderr_thread.join(timeout=0.1)
                message = self._last_ffmpeg_error or f"Could not start recording from {self.input_device}."
                self._fail_recording_start(process, output_path)
                logger.error(
                    "Recording FFmpeg exited before audio was ready pid=%s path=%s error=%s",
                    process.pid,
                    output_path,
                    message,
                )
                raise DeviceUnavailableError(message)
            if self._recording_ready_event.wait(timeout=0.05):
                if process.poll() is not None:
                    continue
                logger.info(
                    "Recording audio ready pid=%s path=%s startup_seconds=%.3f audio_seconds=%.3f",
                    process.pid,
                    output_path,
                    monotonic() - startup_started_at,
                    self._audio_progress_seconds,
                )
                return

        self._fail_recording_start(process, output_path)
        logger.error(
            "Recording audio readiness timed out pid=%s path=%s timeout_seconds=%.1f",
            process.pid,
            output_path,
            self.recording_ready_timeout_seconds,
        )
        raise DeviceUnavailableError(
            f"Timed out waiting for audio from {self.input_device} after "
            f"{self.recording_ready_timeout_seconds:g} seconds."
        )

    def stop_recording(self) -> RecordingStatus:
        process = self._process
        if process is None:
            raise DeviceUnavailableError("No recording process is active.")
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
        status = self.status(recording_override=False, pid_override=None)
        self.reset_recording_state()
        return status

    def start_metering(self) -> None:
        if self._process is not None:
            self._metering_enabled = True
            return
        if self._monitor_process is not None:
            self._metering_enabled = True
            return
        command = self.build_metering_command()
        process = self.start_ffmpeg_process(command)
        self._monitor_process = process
        self._metering_enabled = True
        self._meter_state = self.idle_meter_state(recording=False)
        self.start_stderr_reader(process, source="metering")

    def stop_metering(self) -> None:
        if self._process is not None:
            self._metering_enabled = False
            self._meter_state = self.idle_meter_state(recording=False)
            return
        if self._monitor_process is None:
            raise NotMeteringError("Metering is not active.")
        self._metering_enabled = False
        self.stop_monitor()

    def check_device_available(self, *, midi_online: bool, force: bool = False) -> tuple[bool, str | None]:
        if not self.device_check_enabled:
            return True, None
        if self._process is not None or self._monitor_process is not None:
            self._device_available = True
            self._device_error = None
            self._device_check_time = monotonic()
            return True, None
        if midi_online and not force:
            self._device_available = True
            self._device_error = None
            self._device_check_time = monotonic()
            return True, None

        now = monotonic()
        if (
            not force
            and self._device_check_time is not None
            and now - self._device_check_time < self.device_check_cache_seconds
        ):
            return self._device_available, self._device_error

        command = self.build_device_check_command()
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.device_check_timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            self._device_available = False
            self._device_error = f"{self.ffmpeg_bin} was not found."
        except subprocess.TimeoutExpired:
            self._device_available = False
            self._device_error = f"Timed out checking {self.input_device}."
        else:
            self._device_available = result.returncode == 0
            self._device_error = None if result.returncode == 0 else self.clean_error(result.stderr)
        self._device_check_time = now
        return self._device_available, self._device_error

    def clear_if_process_exited(self, on_recording_exit: Callable[[], None]) -> None:
        if self._process is not None and self._process.poll() is not None:
            logger.error(
                "Recording FFmpeg exited unexpectedly pid=%s path=%s error=%s",
                self._process.pid,
                self._current_path,
                self._last_ffmpeg_error or "unknown",
            )
            on_recording_exit()
            self.reset_recording_state()
        if self._monitor_process is not None and self._monitor_process.poll() is not None:
            self._monitor_process = None
            self._metering_enabled = False
        if self._process is None and self._monitor_process is None:
            self._meter_state = self.idle_meter_state()

    def status(
        self,
        *,
        recording_override: bool | None = None,
        pid_override: int | None = None,
        device_available: bool = True,
        device_error: str | None = None,
        prolink_metadata: dict[str, object] | None = None,
    ) -> RecordingStatus:
        process = self._process
        path = self._current_path
        is_recording = process is not None if recording_override is None else recording_override
        pid = process.pid if process is not None else None
        if pid_override is not None or recording_override is False:
            pid = pid_override
        elapsed = int(self.recording_elapsed_seconds())
        size = self.file_size(path)
        return RecordingStatus(
            recording=is_recording,
            metering_active=self._metering_enabled and (process is not None or self._monitor_process is not None),
            current_filename=path.name if path else None,
            pid=pid,
            elapsed_seconds=elapsed,
            current_file_size=size,
            device_available=device_available,
            device_error=device_error,
            prolink_metadata=prolink_metadata,
        )

    def meter_payload(self) -> dict[str, object]:
        recording_active = self._process is not None
        source_active = self._process is not None or self._monitor_process is not None
        metering_active = self._metering_enabled and source_active
        state = self.meter_state() if metering_active else self.idle_meter_state(recording=False)
        return {
            "recording": recording_active,
            "metering": metering_active,
            "channels": {name: channel.__dict__ for name, channel in state.channels.items()},
            "updated_at": state.updated_at if metering_active else None,
        }

    def meter_state(self) -> MeterState:
        return self._meter_state

    def stop_monitor(self) -> None:
        process = self._monitor_process
        if process is None:
            return
        stop_started_at = monotonic()
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        logger.info(
            "Live metering FFmpeg stopped pid=%s duration_seconds=%.3f",
            process.pid,
            monotonic() - stop_started_at,
        )
        self._monitor_process = None
        if self._process is None:
            self._meter_state = self.idle_meter_state()

    def recording_elapsed_seconds(self, observed_at: float | None = None) -> float:
        progress_observed_at = self._audio_progress_observed_at
        if progress_observed_at is None:
            return 0.0
        if observed_at is None:
            observed_at = monotonic()
        return max(0.0, self._audio_progress_seconds + observed_at - progress_observed_at)

    def recording_started_at_utc(self) -> datetime | None:
        return self._started_at_utc

    def file_size(self, path: Path | None) -> int:
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def start_ffmpeg_process(
        self,
        command: list[str],
        *,
        capture_stdout: bool = False,
        wait_for_grace: bool = True,
    ) -> subprocess.Popen[Any]:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise DeviceUnavailableError(str(exc)) from exc
        if not wait_for_grace:
            return process
        deadline = monotonic() + self.process_start_grace_seconds
        while monotonic() < deadline:
            if process.poll() is not None:
                _, stderr_output = process.communicate(timeout=0.1)
                raise DeviceUnavailableError(self.clean_error(stderr_output))
            sleep(0.05)
        return process

    def start_progress_reader(self, process: subprocess.Popen[Any]) -> None:
        thread = threading.Thread(target=self.read_ffmpeg_progress, args=(process,), daemon=True)
        self._progress_thread = thread
        thread.start()

    def read_ffmpeg_progress(self, process: subprocess.Popen[Any]) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        for line in stdout:
            key, separator, raw_value = line.strip().partition("=")
            if not separator or key != "out_time_us":
                continue
            try:
                audio_seconds = int(raw_value) / 1_000_000
            except ValueError:
                continue
            if audio_seconds <= 0 or self._process is not process or process.poll() is not None:
                continue
            observed_at = monotonic()
            self._audio_progress_seconds = audio_seconds
            self._audio_progress_observed_at = observed_at
            if self._started_at_monotonic is None:
                self._started_at_monotonic = observed_at - audio_seconds
                self._started_at_utc = datetime.now(timezone.utc) - timedelta(seconds=audio_seconds)
            self._recording_ready_event.set()

    def start_stderr_reader(self, process: subprocess.Popen[Any], *, source: str) -> None:
        thread = threading.Thread(target=self.read_ffmpeg_stderr, args=(process, source), daemon=True)
        self._stderr_thread = thread
        thread.start()

    def read_ffmpeg_stderr(self, process: subprocess.Popen[Any], source: str) -> None:
        parser = AstatsParser()
        stderr = process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                stripped = line.strip()
                if stripped:
                    self._last_ffmpeg_error = stripped[-300:]
                meter_state = parser.parse_line(line)
                if meter_state is None:
                    continue
                if source == "recording" and self._process is process:
                    if self._metering_enabled:
                        self._meter_state = meter_state
                if source == "metering" and self._monitor_process is process:
                    if self._metering_enabled:
                        self._meter_state = MeterState(recording=False, channels=meter_state.channels, updated_at=meter_state.updated_at)
        finally:
            if source == "recording" and self._process is process and process.poll() is not None:
                self._meter_state = self.idle_meter_state()
            if source == "metering" and self._monitor_process is process and process.poll() is not None:
                self._meter_state = self.idle_meter_state()

    def build_ffmpeg_command(self, output_path: Path) -> list[str]:
        format_spec = recording_format_for_filename(output_path)
        return [
            self.ffmpeg_bin,
            "-nostats",
            "-stats_period",
            "0.1",
            "-progress",
            "pipe:1",
            "-f",
            "alsa",
            "-channels",
            "12",
            "-sample_rate",
            "48000",
            "-sample_fmt",
            "s32",
            "-i",
            self.input_device,
            "-filter_complex",
            (
                "pan=stereo|c0=c10|c1=c11,"
                "astats=metadata=1:reset=1,"
                "ametadata=mode=print:key=lavfi.astats.1.Peak_level,"
                "ametadata=mode=print:key=lavfi.astats.1.RMS_level,"
                "ametadata=mode=print:key=lavfi.astats.2.Peak_level,"
                "ametadata=mode=print:key=lavfi.astats.2.RMS_level"
            ),
            *format_spec.ffmpeg_args,
            str(output_path),
        ]

    def reset_recording_state(self) -> None:
        self._process = None
        self._current_path = None
        self._started_at_monotonic = None
        self._started_at_utc = None
        self._audio_progress_seconds = 0.0
        self._audio_progress_observed_at = None
        self._recording_ready_event.clear()
        self._metering_enabled = False
        self._meter_state = self.idle_meter_state()

    def _fail_recording_start(self, process: subprocess.Popen[Any], output_path: Path) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        self.reset_recording_state()
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass

    def build_device_check_command(self) -> list[str]:
        return [
            self.ffmpeg_bin,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "alsa",
            "-channels",
            "12",
            "-sample_rate",
            "48000",
            "-sample_fmt",
            "s32",
            "-i",
            self.input_device,
            "-t",
            "0.2",
            "-f",
            "null",
            "-",
        ]

    def build_metering_command(self) -> list[str]:
        return [
            self.ffmpeg_bin,
            "-f",
            "alsa",
            "-channels",
            "12",
            "-sample_rate",
            "48000",
            "-sample_fmt",
            "s32",
            "-i",
            self.input_device,
            "-filter_complex",
            (
                "pan=stereo|c0=c10|c1=c11,"
                "astats=metadata=1:reset=1,"
                "ametadata=mode=print:key=lavfi.astats.1.Peak_level,"
                "ametadata=mode=print:key=lavfi.astats.1.RMS_level,"
                "ametadata=mode=print:key=lavfi.astats.2.Peak_level,"
                "ametadata=mode=print:key=lavfi.astats.2.RMS_level"
            ),
            "-f",
            "null",
            "-",
        ]

    def list_audio_input_devices(self) -> list[dict[str, object]]:
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, TypeError):
            return []
        if result.returncode != 0:
            return []

        devices: list[dict[str, object]] = []
        pattern = re.compile(
            r"^card\s+(\d+):\s*([^\[]+)\[(.+?)\],\s*device\s+(\d+):\s*([^\[]+)\[(.+?)\]\s*$"
        )
        for line in (result.stdout or "").splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            card_index = int(match.group(1))
            card_id = match.group(2).strip()
            card_name = match.group(3).strip()
            device_index = int(match.group(4))
            device_id = match.group(5).strip()
            device_name = match.group(6).strip()
            device_value = f"plughw:{card_index},{device_index}"
            devices.append(
                {
                    "id": device_value,
                    "card_index": card_index,
                    "device_index": device_index,
                    "card_id": card_id,
                    "card_name": card_name,
                    "device_id": device_id,
                    "device_name": device_name,
                    "label": f"{device_value} — {card_name} / {device_name}",
                }
            )
        return devices

    def clean_error(self, stderr: str | None) -> str:
        message = (stderr or "").strip().splitlines()
        if not message:
            return f"Could not open {self.input_device}."
        return message[-1][-300:]

    @staticmethod
    def idle_meter_state(recording: bool = False) -> MeterState:
        from app.services.models import MeterChannel

        return MeterState(
            recording=recording,
            channels={
                "left": MeterChannel(peak_db=None, rms_db=None),
                "right": MeterChannel(peak_db=None, rms_db=None),
            },
            updated_at=None,
        )
