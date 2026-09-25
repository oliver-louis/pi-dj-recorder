import pytest


@pytest.fixture(autouse=True)
def assume_recording_encoders_available(monkeypatch):
    capabilities = [
        {"id": "wav", "label": "WAV — 24-bit lossless", "available": True, "reason": None},
        {"id": "flac", "label": "FLAC — 24-bit lossless", "available": True, "reason": None},
        {"id": "mp3", "label": "MP3 — 320 kbps", "available": True, "reason": None},
    ]
    monkeypatch.setattr(
        "app.services.recorder.recording_format_capabilities",
        lambda ffmpeg_bin: [dict(item) for item in capabilities],
    )
