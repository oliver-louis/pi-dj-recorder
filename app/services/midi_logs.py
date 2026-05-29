from __future__ import annotations

import json
import signal
import subprocess
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from datetime import datetime, timezone

from app.services.models import MidiChannelState
from app.services.parsers import parse_midi_line
from app.services.recordings_store import RecordingsStore


class MidiLoggingService:
    def __init__(
        self,
        *,
        store: RecordingsStore,
        midi_capture_bin: str,
        midi_port: str,
        resolve_port: Callable[[], str | None],
        onair_threshold: int,
    ) -> None:
        self.store = store
        self.midi_capture_bin = midi_capture_bin
        self.midi_port = midi_port
        self.resolve_port = resolve_port
        self.onair_threshold = onair_threshold
        self._midi_process: subprocess.Popen[Any] | None = None
        self._midi_log_path: Path | None = None
        self._midi_started_at_monotonic: float | None = None
        self._midi_reader_thread: threading.Thread | None = None
        self._onair_log_path: Path | None = None
        self._onair_channel_states: dict[str, bool] | None = None

    def start_capture(self, recording_path: Path) -> None:
        self.stop_capture()
        log_path = self.store.midi_log_path_for_name(recording_path.name)
        try:
            process = subprocess.Popen(
                self.build_midi_command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError:
            self._midi_process = None
            self._midi_log_path = None
            self._midi_started_at_monotonic = None
            return
        self._midi_process = process
        self._midi_log_path = log_path
        self._midi_started_at_monotonic = monotonic()
        thread = threading.Thread(target=self._read_midi_stream, args=(process, log_path), daemon=True)
        self._midi_reader_thread = thread
        thread.start()

    def stop_capture(self) -> None:
        process = self._midi_process
        if process is None:
            self._midi_log_path = None
            self._midi_started_at_monotonic = None
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self._midi_process = None
        self._midi_log_path = None
        self._midi_started_at_monotonic = None

    def clear_if_capture_exited(self) -> None:
        if self._midi_process is not None and self._midi_process.poll() is not None:
            self._midi_process = None
            self._midi_log_path = None
            self._midi_started_at_monotonic = None

    def start_onair_log(self, recording_path: Path, channel_states: dict[str, MidiChannelState]) -> None:
        self._onair_log_path = self.store.onair_log_path_for_name(recording_path.name)
        self._onair_channel_states = {name: state.on_air for name, state in channel_states.items()}
        self.write_onair_event(
            {
                "type": "midi_logging_started",
                "recording_filename": recording_path.name,
                "threshold": self.onair_threshold,
                "time_seconds": 0.0,
                "ts_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        for state in channel_states.values():
            if not state.on_air:
                continue
            self.write_onair_event(
                {
                    "type": "channel_in",
                    "channel": state.controller_id + 1,
                    "value": state.value,
                    "time_seconds": 0.0,
                }
            )

    def stop_onair_log(self, elapsed_seconds: float) -> None:
        if self._onair_log_path is None:
            self._onair_channel_states = None
            return
        self.write_onair_event({"type": "midi_logging_stopped", "time_seconds": elapsed_seconds})
        self._onair_log_path = None
        self._onair_channel_states = None

    def apply_daemon_payload(self, payload: dict[str, object], *, current_state: MidiChannelState, next_on_air: bool, elapsed_seconds: float) -> None:
        if self._onair_channel_states is None:
            return
        previous_on_air = self._onair_channel_states.get(current_state.channel_name, current_state.on_air)
        if previous_on_air == next_on_air:
            self._onair_channel_states[current_state.channel_name] = next_on_air
            return
        value = payload.get("value")
        if not isinstance(value, int):
            return
        self.write_onair_event(
            {
                "type": "channel_in" if next_on_air else "channel_out",
                "channel": current_state.controller_id + 1,
                "value": value,
                "time_seconds": elapsed_seconds,
            }
        )
        self._onair_channel_states[current_state.channel_name] = next_on_air

    def write_onair_event(self, payload: dict[str, object]) -> None:
        if self._onair_log_path is None:
            return
        with self._onair_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    def parse_midi_line(self, line: str) -> dict[str, object]:
        return parse_midi_line(line, source_port=self.midi_port, elapsed_ms=self.midi_elapsed_ms())

    def midi_elapsed_ms(self) -> int:
        if self._midi_started_at_monotonic is None:
            return 0
        return max(0, int((monotonic() - self._midi_started_at_monotonic) * 1000))

    def build_midi_command(self, resolved_port: str | None = None) -> list[str]:
        port = resolved_port if resolved_port is not None else self.resolve_port()
        if port is None:
            raise FileNotFoundError("No matching MIDI input port is currently available.")
        return [self.midi_capture_bin, "-p", port]

    def _read_midi_stream(self, process: subprocess.Popen[Any], log_path: Path) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                for line in stream:
                    payload = self.parse_midi_line(line)
                    handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        finally:
            if self._midi_process is process and process.poll() is not None:
                self._midi_process = None
                self._midi_log_path = None
                self._midi_started_at_monotonic = None
