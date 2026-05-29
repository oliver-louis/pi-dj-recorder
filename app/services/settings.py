from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    midi_port: str
    midi_port_name_hint: str
    input_device: str
    onair_threshold: int
    prolink_onair_enabled: bool
    prolink_onair_threshold: int
    prolink_onair_channel_to_player: dict[str, int]
    default_mix_prefix: str
    track_id_merge_gap_seconds: float
    auto_enable_metering: bool
    theme: str
    confirm_delete_recordings: bool
    stop_discard_countdown_seconds: int


class SettingsStore:
    def __init__(self, path: Path, defaults: AppSettings) -> None:
        self.path = Path(path)
        self.defaults = defaults

    def load(self) -> AppSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return self.defaults
        return AppSettings(
            midi_port=str(raw.get("midi_port") or self.defaults.midi_port),
            midi_port_name_hint=str(raw.get("midi_port_name_hint") or self.defaults.midi_port_name_hint),
            input_device=str(raw.get("input_device") or self.defaults.input_device),
            onair_threshold=int(raw.get("onair_threshold", self.defaults.onair_threshold)),
            prolink_onair_enabled=bool(raw.get("prolink_onair_enabled", self.defaults.prolink_onair_enabled)),
            prolink_onair_threshold=int(
                raw.get("prolink_onair_threshold", self.defaults.prolink_onair_threshold)
            ),
            prolink_onair_channel_to_player=self._load_channel_to_player(
                raw.get("prolink_onair_channel_to_player"),
                self.defaults.prolink_onair_channel_to_player,
            ),
            default_mix_prefix=str(raw.get("default_mix_prefix") or self.defaults.default_mix_prefix),
            track_id_merge_gap_seconds=float(
                raw.get("track_id_merge_gap_seconds", self.defaults.track_id_merge_gap_seconds)
            ),
            auto_enable_metering=bool(raw.get("auto_enable_metering", self.defaults.auto_enable_metering)),
            theme=str(raw.get("theme") or self.defaults.theme),
            confirm_delete_recordings=bool(
                raw.get("confirm_delete_recordings", self.defaults.confirm_delete_recordings)
            ),
            stop_discard_countdown_seconds=int(
                raw.get("stop_discard_countdown_seconds", self.defaults.stop_discard_countdown_seconds)
            ),
        )

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _load_channel_to_player(raw: object, default: dict[str, int]) -> dict[str, int]:
        if not isinstance(raw, dict):
            return dict(default)
        loaded: dict[str, int] = dict(default)
        for channel, player in raw.items():
            try:
                loaded[str(int(channel))] = int(player)
            except (TypeError, ValueError):
                continue
        return loaded
