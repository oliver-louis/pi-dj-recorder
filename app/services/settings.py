from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppSettings:
    midi_port: str
    midi_port_name_hint: str
    input_device: str


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
        )

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
