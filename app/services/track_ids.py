from __future__ import annotations

import json

from app.services.errors import TrackIdExportError
from app.services.recordings_store import RecordingsStore


class TrackIdExporter:
    def __init__(self, store: RecordingsStore) -> None:
        self.store = store

    def export_for_recording(self, filename: str) -> tuple[str, bytes]:
        path = self.store.onair_log_path_for_recording(filename)
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise TrackIdExportError("On-air log is malformed.") from exc
        if not events:
            raise TrackIdExportError("On-air log is empty.")

        sessions = self.track_sessions_from_onair_events(events)
        export_payload = [
            {
                "title": self.track_title_for_channel(session["channel"]),
                "artist": "",
                "links": {},
                "start": self.format_track_time(float(session["start"])),
                "end": self.format_track_time(float(session["end"])),
            }
            for session in sessions
        ]
        export_name = f"{filename[:-4]}.track-ids.json"
        return export_name, json.dumps(export_payload, indent=2).encode("utf-8")

    @staticmethod
    def track_title_for_channel(channel: int) -> str:
        mapping = {1: "Turntable 1", 2: "CDJ 1", 3: "CDJ 2", 4: "Turntable 2"}
        return mapping.get(channel, f"Channel {channel}")

    @staticmethod
    def format_track_time(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def track_sessions_from_onair_events(self, events: list[dict[str, object]]) -> list[dict[str, float | int]]:
        stop_time = None
        sessions: list[dict[str, float | int]] = []
        state: dict[int, dict[str, float | bool | None]] = {}

        for event in events:
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise TrackIdExportError("On-air log is malformed.")
            if event_type == "midi_logging_stopped":
                stop_time = self.event_time_seconds(event)
                break
            if event_type == "midi_logging_started":
                continue
            if event_type not in {"channel_in", "channel_out"}:
                continue
            channel = event.get("channel")
            if not isinstance(channel, int):
                raise TrackIdExportError("On-air log is malformed.")
            time_seconds = self.event_time_seconds(event)
            channel_state = state.setdefault(channel, {"active": False, "start": None, "pending_out": None})

            if event_type == "channel_in":
                pending_out = channel_state["pending_out"]
                if channel_state["active"]:
                    channel_state["pending_out"] = None
                    continue
                if isinstance(pending_out, (int, float)) and isinstance(channel_state["start"], (int, float)):
                    gap = time_seconds - float(pending_out)
                    if gap <= 10.0:
                        channel_state["active"] = True
                        channel_state["pending_out"] = None
                        continue
                    sessions.append({"channel": channel, "start": float(channel_state["start"]), "end": float(pending_out)})
                channel_state["active"] = True
                channel_state["start"] = time_seconds
                channel_state["pending_out"] = None
                continue

            if channel_state["active"]:
                channel_state["active"] = False
                channel_state["pending_out"] = time_seconds

        if stop_time is None:
            raise TrackIdExportError("On-air log is incomplete.")

        for channel, channel_state in state.items():
            start = channel_state["start"]
            if not isinstance(start, (int, float)):
                continue
            pending_out = channel_state["pending_out"]
            if channel_state["active"]:
                end = stop_time
            elif isinstance(pending_out, (int, float)):
                end = float(pending_out)
            else:
                continue
            if end <= float(start):
                continue
            sessions.append({"channel": channel, "start": float(start), "end": float(end)})

        sessions.sort(key=lambda session: (float(session["start"]), int(session["channel"])))
        if not sessions:
            raise TrackIdExportError("No track sessions could be inferred from the on-air log.")
        return sessions

    @staticmethod
    def event_time_seconds(event: dict[str, object]) -> float:
        value = event.get("time_seconds")
        if not isinstance(value, (int, float)):
            raise TrackIdExportError("On-air log is malformed.")
        return float(value)
