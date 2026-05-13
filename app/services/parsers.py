from __future__ import annotations

import re
from datetime import datetime, timezone

from app.services.models import MeterChannel, MeterState


ASTATS_CHANNEL = re.compile(r"(?:^|\]\s*)Channel:\s*(\d+)\s*$")
ASTATS_VALUE = re.compile(r"(Peak level dB|RMS level dB):\s*([-+]?inf|[-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
ASTATS_METADATA = re.compile(
    r"lavfi\.astats\.(1|2)\.(Peak_level|RMS_level)=([-+]?inf|[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
WAVEFORM_RMS_METADATA = re.compile(r"lavfi\.astats\.1\.RMS_level=([-+]?inf|[-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
ASEQDUMP_LINE = re.compile(r"^\s*(\d+:\d+)\s+(.+?)\s+ch=(\d+)\s*(.*)$", re.IGNORECASE)
ASEQDUMP_CONTROLLER_LINE = re.compile(
    r"^\s*(\d+:\d+)\s+(.+?)\s+(\d+),\s*controller\s+(\d+),\s*value\s+(\d+)\s*$",
    re.IGNORECASE,
)
ASEQDUMP_CONTROL = re.compile(r"\b(?:control|param)=(\d+)\b", re.IGNORECASE)
ASEQDUMP_VALUE = re.compile(r"\bvalue=(\d+)\b", re.IGNORECASE)
ASEQDUMP_SOURCE = re.compile(r"^\s*(\d+:\d+)", re.IGNORECASE)
ASEQDUMP_EVENT = re.compile(r"^\s*\d+:\d+\s+([a-z ]+?)(?:\s+ch=|\s+\d+,|\s*$)", re.IGNORECASE)
ASEQDUMP_ALT_CHANNEL = re.compile(r"\b(\d+),\s*controller\b", re.IGNORECASE)
ASEQDUMP_ALT_CONTROL = re.compile(r"\bcontroller\s+(\d+)\b", re.IGNORECASE)
ASEQDUMP_ALT_VALUE = re.compile(r"\bvalue\s+(\d+)\b", re.IGNORECASE)
ASEQDUMP_LIST_PORT = re.compile(r"^\s*(\d+:\d+)\s+(.+?)\s{2,}(.+?)\s*$")


class AstatsParser:
    def __init__(self) -> None:
        self._current_channel: str | None = None
        self._values: dict[str, dict[str, float | None]] = {
            "left": {"peak_db": None, "rms_db": None},
            "right": {"peak_db": None, "rms_db": None},
        }

    def parse_line(self, line: str) -> MeterState | None:
        metadata_match = ASTATS_METADATA.search(line)
        if metadata_match:
            channel = {"1": "left", "2": "right"}[metadata_match.group(1)]
            key = "peak_db" if metadata_match.group(2).lower().startswith("peak") else "rms_db"
            self._values[channel][key] = self._parse_db(metadata_match.group(3))
            return self._meter_state()

        channel_match = ASTATS_CHANNEL.search(line)
        if channel_match:
            self._current_channel = {"1": "left", "2": "right"}.get(channel_match.group(1))
            return None
        if re.search(r"(?:^|\]\s*)Overall\s*$", line):
            self._current_channel = None
            return None

        value_match = ASTATS_VALUE.search(line)
        if not value_match or self._current_channel is None:
            return None

        key = "peak_db" if value_match.group(1).lower().startswith("peak") else "rms_db"
        self._values[self._current_channel][key] = self._parse_db(value_match.group(2))
        return self._meter_state()

    def _meter_state(self) -> MeterState:
        return MeterState(
            recording=True,
            channels={
                "left": MeterChannel(**self._values["left"]),
                "right": MeterChannel(**self._values["right"]),
            },
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _parse_db(value: str) -> float | None:
        if value.lower() in {"-inf", "+inf", "inf"}:
            return None
        try:
            return float(value)
        except ValueError:
            return None


def parse_midi_line(line: str, *, source_port: str, elapsed_ms: int) -> dict[str, object]:
    stripped = line.strip()
    payload: dict[str, object] = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": elapsed_ms,
        "source": source_port,
        "raw": stripped,
    }
    controller_line_match = ASEQDUMP_CONTROLLER_LINE.match(stripped)
    if controller_line_match:
        payload["source"] = controller_line_match.group(1)
        payload["event_type"] = controller_line_match.group(2).strip().lower().replace(" ", "_")
        payload["channel"] = int(controller_line_match.group(3))
        payload["control"] = int(controller_line_match.group(4))
        payload["value"] = int(controller_line_match.group(5))
        return payload

    match = ASEQDUMP_LINE.match(stripped)
    if match:
        payload["source"] = match.group(1)
        payload["event_type"] = match.group(2).strip().lower().replace(" ", "_")
        payload["channel"] = int(match.group(3))
        tail = match.group(4)
    else:
        source_match = ASEQDUMP_SOURCE.search(stripped)
        event_match = ASEQDUMP_EVENT.search(stripped)
        channel_match = ASEQDUMP_ALT_CHANNEL.search(stripped)
        if source_match:
            payload["source"] = source_match.group(1)
        if event_match:
            payload["event_type"] = event_match.group(1).strip().lower().replace(" ", "_")
        if channel_match:
            payload["channel"] = int(channel_match.group(1))
        tail = stripped

    control_match = ASEQDUMP_CONTROL.search(tail)
    value_match = ASEQDUMP_VALUE.search(tail)
    if control_match is None:
        control_match = ASEQDUMP_ALT_CONTROL.search(tail)
    if value_match is None:
        value_match = ASEQDUMP_ALT_VALUE.search(tail)
    if control_match:
        payload["control"] = int(control_match.group(1))
    if value_match:
        payload["value"] = int(value_match.group(1))
    return payload
