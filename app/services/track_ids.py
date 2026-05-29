from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.services.errors import TrackIdExportError
from app.services.recordings_store import RecordingsStore


class TrackIdExporter:
    def __init__(self, store: RecordingsStore) -> None:
        self.store = store

    def export_for_recording(
        self,
        filename: str,
        *,
        merge_gap_seconds: float = 10.0,
        metadata_log_path: Path | None = None,
    ) -> tuple[str, bytes]:
        path = self.store.onair_log_path_for_recording(filename)
        try:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise TrackIdExportError("On-air log is malformed.") from exc
        if not events:
            raise TrackIdExportError("On-air log is empty.")

        sessions = self.track_sessions_from_onair_events(events, merge_gap_seconds=merge_gap_seconds)
        recording_started_at = self.recording_started_at(events)
        metadata_events = self.metadata_events(metadata_log_path)
        export_payload = [
            self.track_id_payload_for_session(
                session,
                metadata_events=metadata_events,
                recording_started_at=recording_started_at,
            )
            for session in sessions
        ]
        export_name = f"{filename[:-4]}.track-ids.json"
        return export_name, json.dumps(export_payload, indent=2).encode("utf-8")

    def track_id_payload_for_session(
        self,
        session: dict[str, float | int],
        *,
        metadata_events: list[dict[str, object]],
        recording_started_at: datetime | None,
    ) -> dict[str, object]:
        channel = int(session["channel"])
        match = self.metadata_for_session(
            channel,
            float(session["start"]),
            metadata_events=metadata_events,
            recording_started_at=recording_started_at,
        )
        return {
            "title": str(match.get("title") or self.track_title_for_channel(channel)) if match else self.track_title_for_channel(channel),
            "artist": str(match.get("artist") or "") if match else "",
            "links": {},
            "start": self.format_track_time(float(session["start"])),
            "end": self.format_track_time(float(session["end"])),
        }

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

    def track_sessions_from_onair_events(
        self,
        events: list[dict[str, object]],
        *,
        merge_gap_seconds: float = 10.0,
    ) -> list[dict[str, float | int]]:
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
                    if gap <= merge_gap_seconds:
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
    def recording_started_at(events: list[dict[str, object]]) -> datetime | None:
        for event in events:
            if event.get("type") != "midi_logging_started":
                continue
            ts_utc = event.get("ts_utc")
            if not isinstance(ts_utc, str):
                return None
            try:
                return datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def metadata_events(path: Path | None) -> list[dict[str, object]]:
        if path is None or not path.is_file():
            return []
        events: list[dict[str, object]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "track_loaded":
                events.append(event)
        return events

    def metadata_for_session(
        self,
        channel: int,
        start_seconds: float,
        *,
        metadata_events: list[dict[str, object]],
        recording_started_at: datetime | None,
    ) -> dict[str, object] | None:
        if recording_started_at is None:
            return None
        session_started_at = recording_started_at + timedelta(seconds=start_seconds)
        best: tuple[datetime, dict[str, object]] | None = None
        for event in metadata_events:
            if event.get("channel") != channel:
                continue
            title = event.get("title")
            artist = event.get("artist")
            if not isinstance(title, str) or not title.strip():
                continue
            if artist is not None and not isinstance(artist, str):
                continue
            ts_utc = event.get("ts_utc")
            if not isinstance(ts_utc, str):
                continue
            try:
                event_time = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
            except ValueError:
                continue
            if event_time > session_started_at:
                continue
            if best is None or event_time > best[0]:
                best = (event_time, event)
        return best[1] if best else None

    @staticmethod
    def event_time_seconds(event: dict[str, object]) -> float:
        value = event.get("time_seconds")
        if not isinstance(value, (int, float)):
            raise TrackIdExportError("On-air log is malformed.")
        return float(value)
