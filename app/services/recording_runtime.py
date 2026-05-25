from __future__ import annotations

import re
import signal
import subprocess
import threading
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable

from app.services.errors import DeviceUnavailableError, NotMeteringError
from app.services.models import MeterState, RecordingStatus
from app.services.parsers import AstatsParser


class RecordingRuntimeService:
    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        input_device: str,
        stop_timeout_seconds: float,
        process_start_grace_seconds: float,
        device_check_timeout_seconds: float,
        device_check_cache_seconds: float,
        device_check_enabled: bool,
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.input_device = input_device
        self.stop_timeout_seconds = stop_timeout_seconds
        self.process_start_grace_seconds = process_start_grace_seconds
        self.device_check_timeout_seconds = device_check_timeout_seconds
        self.device_check_cache_seconds = device_check_cache_seconds
        self.device_check_enabled = device_check_enabled
        self._process: subprocess.Popen[Any] | None = None
        self._monitor_process: subprocess.Popen[Any] | None = None
        self._current_path: Path | None = None
        self._started_at_monotonic: float | None = None
        self._stderr_thread: threading.Thread | None = None
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

    def start_recording(self, output_path: Path) -> None:
        command = self.build_ffmpeg_command(output_path)
        process = self.start_ffmpeg_process(command)
        self._process = process
        self._current_path = output_path
        self._started_at_monotonic = monotonic()
        self._metering_enabled = True
        self._meter_state = self.idle_meter_state(recording=True)
        self.start_stderr_reader(process, source="recording")

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
        self._process = None
        self._current_path = None
        self._started_at_monotonic = None
        self._metering_enabled = False
        self._meter_state = self.idle_meter_state()
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
            self._process = None
            self._current_path = None
            self._started_at_monotonic = None
            on_recording_exit()
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
    ) -> RecordingStatus:
        process = self._process
        path = self._current_path
        is_recording = process is not None if recording_override is None else recording_override
        pid = process.pid if process is not None else None
        if pid_override is not None or recording_override is False:
            pid = pid_override
        elapsed = 0
        if self._started_at_monotonic is not None:
            elapsed = int(monotonic() - self._started_at_monotonic)
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
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self._monitor_process = None
        if self._process is None:
            self._meter_state = self.idle_meter_state()

    def recording_elapsed_seconds(self) -> float:
        if self._started_at_monotonic is None:
            return 0.0
        return max(0.0, monotonic() - self._started_at_monotonic)

    def file_size(self, path: Path | None) -> int:
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def start_ffmpeg_process(self, command: list[str]) -> subprocess.Popen[Any]:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise DeviceUnavailableError(str(exc)) from exc
        deadline = monotonic() + self.process_start_grace_seconds
        while monotonic() < deadline:
            if process.poll() is not None:
                _, stderr_output = process.communicate(timeout=0.1)
                raise DeviceUnavailableError(self.clean_error(stderr_output))
            sleep(0.05)
        return process

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
            "-c:a",
            "pcm_s24le",
            str(output_path),
        ]

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
