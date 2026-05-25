from __future__ import annotations

import re
import signal
import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.services.models import MidiChannelState
from app.services.parsers import ASEQDUMP_LIST_PORT, parse_midi_line


MISSING = object()


class MidiDaemonService:
    def __init__(
        self,
        *,
        midi_capture_bin: str,
        midi_port: str,
        midi_port_name_hint: str,
        onair_threshold: int,
        on_channel_payload: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.midi_capture_bin = midi_capture_bin
        self.midi_port = midi_port
        self.midi_port_name_hint = midi_port_name_hint
        self.onair_threshold = onair_threshold
        self.on_channel_payload = on_channel_payload
        self._daemon_midi_process: subprocess.Popen[Any] | None = None
        self._daemon_midi_reader_thread: threading.Thread | None = None
        self._daemon_watchdog_thread: threading.Thread | None = None
        self._daemon_stop_event = threading.Event()
        self._midi_online = False
        self._midi_error: str | None = None
        self._midi_updated_at: str | None = None
        self._daemon_port_in_use: str | None = None
        self._midi_channels: dict[str, MidiChannelState] = {
            f"CH{index + 1}": MidiChannelState(
                channel_name=f"CH{index + 1}",
                controller_id=index,
                value=0,
                on_air=False,
                last_changed_at=None,
            )
            for index in range(4)
        }
        self._controller_to_channel = {index: f"CH{index + 1}" for index in range(4)}

    @property
    def daemon_process(self) -> subprocess.Popen[Any] | None:
        return self._daemon_midi_process

    @daemon_process.setter
    def daemon_process(self, value: subprocess.Popen[Any] | None) -> None:
        self._daemon_midi_process = value

    @property
    def daemon_port_in_use(self) -> str | None:
        return self._daemon_port_in_use

    @daemon_port_in_use.setter
    def daemon_port_in_use(self, value: str | None) -> None:
        self._daemon_port_in_use = value

    @property
    def midi_online(self) -> bool:
        return self._midi_online

    @midi_online.setter
    def midi_online(self, value: bool) -> None:
        self._midi_online = value

    def start(self) -> None:
        self._daemon_stop_event.clear()
        resolved_port = self.resolve_midi_port()
        self.ensure_locked(resolved_port)
        if self._daemon_watchdog_thread is None or not self._daemon_watchdog_thread.is_alive():
            thread = threading.Thread(target=self._watchdog_loop, daemon=True)
            self._daemon_watchdog_thread = thread
            thread.start()

    def shutdown(self) -> None:
        self._daemon_stop_event.set()
        self.stop_locked()

    def state_payload(self) -> dict[str, object]:
        return {
            "midi_online": self._midi_online,
            "midi_error": self._midi_error,
            "updated_at": self._midi_updated_at,
            "threshold": self.onair_threshold,
            "channels": {name: state.__dict__ for name, state in self._midi_channels.items()},
        }

    def channels(self) -> dict[str, MidiChannelState]:
        return self._midi_channels

    def clear_if_process_exited(self) -> None:
        if self._daemon_midi_process is not None and self._daemon_midi_process.poll() is not None:
            self._daemon_midi_process = None
            self._midi_online = False

    def apply_payload_locked(self, payload: dict[str, object]) -> tuple[MidiChannelState | None, bool | None]:
        self._midi_updated_at = str(payload.get("ts_utc"))
        control = payload.get("control")
        value = payload.get("value")
        if not isinstance(control, int) or not isinstance(value, int):
            return None, None
        channel_name = self._controller_to_channel.get(control)
        if channel_name is None:
            return None, None
        current = self._midi_channels[channel_name]
        clamped = max(0, min(127, value))
        next_on_air = clamped >= self.onair_threshold
        updated = MidiChannelState(
            channel_name=current.channel_name,
            controller_id=current.controller_id,
            value=clamped,
            on_air=next_on_air,
            last_changed_at=self._midi_updated_at,
        )
        self._midi_channels[channel_name] = updated
        return current, next_on_air

    def parse_midi_line(self, line: str) -> dict[str, object]:
        return parse_midi_line(line, source_port=self.midi_port, elapsed_ms=0)

    def build_midi_command(self, resolved_port: str | None = None) -> list[str]:
        port = resolved_port if resolved_port is not None else self.resolve_midi_port()
        if port is None:
            raise FileNotFoundError("No matching MIDI input port is currently available.")
        return [self.midi_capture_bin, "-p", port]

    def resolve_midi_port(self) -> str | None:
        ports = self.list_midi_ports()
        if ports is None:
            return self.midi_port
        if not ports:
            return None
        for port_id, _, _ in ports:
            if port_id == self.midi_port:
                return port_id
        hint = self.midi_port_name_hint.strip().lower()
        if hint:
            normalized_hint = self.normalize_name(hint)
            for port_id, client_name, port_name in ports:
                haystack = f"{client_name} {port_name}".lower()
                normalized_haystack = self.normalize_name(haystack)
                if hint in haystack or (normalized_hint and normalized_hint in normalized_haystack):
                    return port_id
        return None

    @staticmethod
    def normalize_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    def list_midi_ports(self) -> list[tuple[str, str, str]] | None:
        try:
            result = subprocess.run(
                [self.midi_capture_bin, "-l"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, TypeError):
            return None
        if result.returncode != 0:
            return None
        if not (result.stdout or "").strip():
            return None
        ports: list[tuple[str, str, str]] = []
        for line in (result.stdout or "").splitlines():
            match = ASEQDUMP_LIST_PORT.match(line)
            if not match:
                continue
            ports.append((match.group(1), match.group(2).strip(), match.group(3).strip()))
        return ports

    def ensure_locked(self, resolved_port: str | None | object = MISSING) -> None:
        if resolved_port is MISSING:
            resolved_port = self.resolve_midi_port()
        process = self._daemon_midi_process
        if process is not None and process.poll() is None:
            if resolved_port is None:
                self.stop_locked()
            elif self._daemon_port_in_use is not None and resolved_port != self._daemon_port_in_use:
                self.stop_locked()
            else:
                self._midi_online = True
                self._midi_error = None
                return
        if self._daemon_stop_event.is_set():
            return
        try:
            command = self.build_midi_command(resolved_port if isinstance(resolved_port, str) else None)
        except FileNotFoundError as exc:
            self._daemon_midi_process = None
            self._daemon_port_in_use = None
            self._midi_online = False
            self._midi_error = str(exc)
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
            self._midi_online = False
            self._midi_error = str(exc)
            return
        self._daemon_midi_process = process
        self._daemon_port_in_use = command[-1]
        self._midi_online = True
        self._midi_error = None
        thread = threading.Thread(target=self._read_stream, args=(process,), daemon=True)
        self._daemon_midi_reader_thread = thread
        thread.start()

    def stop_locked(self) -> None:
        process = self._daemon_midi_process
        if process is None:
            self._daemon_port_in_use = None
            self._midi_online = False
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
        self._daemon_midi_process = None
        self._daemon_port_in_use = None
        self._midi_online = False

    def _watchdog_loop(self) -> None:
        while not self._daemon_stop_event.wait(1.0):
            resolved_port = self.resolve_midi_port()
            self.ensure_locked(resolved_port)

    def _read_stream(self, process: subprocess.Popen[Any]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                payload = self.parse_midi_line(line)
                if self._daemon_midi_process is not process:
                    return
                self._midi_updated_at = datetime.now(timezone.utc).isoformat()
                if self.on_channel_payload is not None:
                    self.on_channel_payload(payload)
        finally:
            if self._daemon_midi_process is process:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                self._daemon_midi_process = None
                self._daemon_port_in_use = None
                self._midi_online = False
                self._midi_error = "MIDI daemon disconnected; waiting to reconnect."
