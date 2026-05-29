import json
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from app.recorder import AstatsParser, AlreadyRecordingError, DeviceUnavailableError, NotRecordingError, Recorder, RecorderError, WaveformGenerationError


class FakeProcess:
    next_pid = 1000

    def __init__(self, command, stderr_output="", **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.args = command
        self.returncode = None
        self.stderr_output = stderr_output
        self.signals = []
        self.terminated = False
        self.killed = False
        self.stdout = FakeStream()
        self.stderr = FakeStream()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGINT:
            self.returncode = 0
            self._close_streams()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._close_streams()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._close_streams()

    def communicate(self, input=None, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        self._close_streams()
        return ("", self.stderr_output)

    def _close_streams(self):
        for stream in (self.stdout, self.stderr):
            close = getattr(stream, "close", None)
            if callable(close):
                close()


class FakeStream:
    def __init__(self):
        self._closed = threading.Event()

    def close(self):
        self._closed.set()

    def __iter__(self):
        return self

    def __next__(self):
        self._closed.wait()
        raise StopIteration


def test_start_creates_expected_command(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)

    status = recorder.start()

    assert status.recording is True
    assert status.current_filename.startswith("mix_")
    assert status.current_filename.endswith(".wav")
    assert status.pid == processes[0].pid
    command = processes[0].command
    assert command[:12] == [
        "ffmpeg",
        "-f",
        "alsa",
        "-channels",
        "12",
        "-sample_rate",
        "48000",
        "-sample_fmt",
        "s32",
        "-i",
        "plughw:X2,0",
        "-filter_complex",
    ]
    assert command[12].startswith("pan=stereo|c0=c10|c1=c11,astats=metadata=1:reset=1")
    assert "ametadata=mode=print:key=lavfi.astats.1.Peak_level" in command[12]
    assert "ametadata=mode=print:key=lavfi.astats.2.RMS_level" in command[12]
    assert command[-2:] == ["pcm_s24le", str(tmp_path / status.current_filename)]
    assert any(process.command == ["aseqdump", "-p", "16:0"] for process in processes)


def test_second_start_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)

    recorder.start()

    with pytest.raises(AlreadyRecordingError):
        recorder.start()


def test_stop_sends_sigint(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder.start()

    status = recorder.stop()

    assert processes[0].signals == [signal.SIGINT]
    assert status.recording is False
    assert status.pid is None
    assert status.current_filename is not None


def test_stop_discard_removes_file_and_waveform_cache(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    started = recorder.start()
    recording_path = tmp_path / started.current_filename
    recording_path.write_bytes(b"wav")
    cache_path = tmp_path / ".waveforms" / f"{started.current_filename}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{}")
    midi_path = tmp_path / f"{started.current_filename[:-4]}.midi.jsonl"
    midi_path.write_text("{}\n")
    onair_path = tmp_path / f"{started.current_filename[:-4]}.onair.jsonl"
    onair_path.write_text("{}\n")

    recorder.stop(discard=True)

    assert not recording_path.exists()
    assert not cache_path.exists()
    assert not midi_path.exists()
    assert not onair_path.exists()


def test_stop_without_active_recording_raises(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    with pytest.raises(NotRecordingError):
        recorder.stop()


def test_status_reports_file_size(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    started = recorder.start()
    Path(tmp_path / started.current_filename).write_bytes(b"12345")

    status = recorder.status()

    assert status.recording is True
    assert status.current_file_size == 5
    assert status.elapsed_seconds >= 0


def test_dead_process_is_cleared(monkeypatch, tmp_path):
    process = FakeProcess(["ffmpeg"])
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: process if command and command[0] == "ffmpeg" else FakeProcess(command, **kwargs),
    )
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder.start()
    process.returncode = 1

    status = recorder.status()

    assert status.recording is False
    assert status.current_filename is None
    assert status.pid is None


def test_recent_recordings_filters_and_sorts(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    old_file = tmp_path / "mix_2026-05-06_01-00-00.wav"
    new_file = tmp_path / "mix_2026-05-06_02-00-00.wav"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    (tmp_path / "notes.txt").write_text("skip")

    files = recorder.recent_recordings()

    assert [file.name for file in files] == [new_file.name, old_file.name]


def test_path_for_recording_rejects_invalid_name(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    with pytest.raises(ValueError):
        recorder.path_for_recording("../mix_2026-05-06_01-00-00.wav")


def test_custom_mix_name_is_slugged(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)

    status = recorder.start("Friday Warm Up!")

    assert status.current_filename.startswith("friday-warm-up_")
    assert status.current_filename.endswith(".wav")


def test_blank_mix_name_uses_configured_default_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "midi_port": "16:0",
                "midi_port_name_hint": "XONE:96",
                "input_device": "plughw:X2,0",
                "onair_threshold": 30,
                "default_mix_prefix": "vinyl-set",
            }
        )
    )
    recorder = Recorder(tmp_path, config_path=config_path, device_check_enabled=False)

    status = recorder.start()

    assert status.current_filename.startswith("vinyl-set_")


def test_settings_bootstrap_from_existing_config(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "midi_port": "24:0",
                "midi_port_name_hint": "XONE 96 USB 2 XONE 96 2",
                "input_device": "plughw:2,0",
                "onair_threshold": 42,
                "default_mix_prefix": "vinyl",
                "track_id_merge_gap_seconds": 6,
                "auto_enable_metering": True,
                "theme": "light",
                "confirm_delete_recordings": False,
                "stop_discard_countdown_seconds": 1,
            }
        )
    )

    recorder = Recorder(tmp_path, config_path=tmp_path / "config.json", device_check_enabled=False)

    payload = recorder.settings_payload()

    assert payload["settings"]["midi_port"] == "24:0"
    assert payload["settings"]["input_device"] == "plughw:2,0"
    assert payload["settings"]["onair_threshold"] == 42
    assert payload["settings"]["default_mix_prefix"] == "vinyl"
    assert payload["settings"]["track_id_merge_gap_seconds"] == 6
    assert payload["settings"]["auto_enable_metering"] is True
    assert payload["settings"]["theme"] == "light"
    assert payload["settings"]["confirm_delete_recordings"] is False
    assert payload["settings"]["stop_discard_countdown_seconds"] == 1


def test_settings_bootstrap_preserves_current_defaults_when_missing(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "midi_port": "24:0",
                "midi_port_name_hint": "XONE 96 USB 2 XONE 96 2",
                "input_device": "plughw:2,0",
            }
        )
    )

    recorder = Recorder(tmp_path, config_path=tmp_path / "config.json", device_check_enabled=False)

    settings = recorder.settings_payload()["settings"]

    assert settings["onair_threshold"] == 30
    assert settings["default_mix_prefix"] == "mix"
    assert settings["track_id_merge_gap_seconds"] == 10.0
    assert settings["auto_enable_metering"] is False
    assert settings["theme"] == "dark"
    assert settings["confirm_delete_recordings"] is True
    assert settings["stop_discard_countdown_seconds"] == 3


def test_device_check_reports_unavailable(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stderr="cannot open audio device\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path)

    available, error = recorder.check_device_available(force=True)

    assert available is False
    assert error == "cannot open audio device"


def test_rename_recording_preserves_timestamp(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    original = tmp_path / "mix_2026-05-06_01-00-00.wav"
    original.write_bytes(b"wav")
    (tmp_path / "mix_2026-05-06_01-00-00.midi.jsonl").write_text("{}\n")
    (tmp_path / "mix_2026-05-06_01-00-00.onair.jsonl").write_text("{}\n")

    renamed = recorder.rename_recording(original.name, "Main Room")

    assert renamed.name == "main-room_2026-05-06_01-00-00.wav"
    assert not original.exists()
    assert (tmp_path / renamed.name).exists()
    assert (tmp_path / "main-room_2026-05-06_01-00-00.midi.jsonl").exists()
    assert (tmp_path / "main-room_2026-05-06_01-00-00.onair.jsonl").exists()


def test_delete_recording(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recording = tmp_path / "mix_2026-05-06_01-00-00.wav"
    recording.write_bytes(b"wav")
    midi = tmp_path / "mix_2026-05-06_01-00-00.midi.jsonl"
    midi.write_text("{}\n")
    onair = tmp_path / "mix_2026-05-06_01-00-00.onair.jsonl"
    onair.write_text("{}\n")

    recorder.delete_recording(recording.name)

    assert not recording.exists()
    assert not midi.exists()
    assert not onair.exists()


def test_parse_midi_line_parses_control_change(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    payload = recorder._parse_midi_line("16:0 Control change ch=2 param=11 value=98")

    assert payload["source"] == "16:0"
    assert payload["event_type"] == "control_change"
    assert payload["channel"] == 2
    assert payload["control"] == 11
    assert payload["value"] == 98
    assert payload["raw"] == "16:0 Control change ch=2 param=11 value=98"


def test_parse_midi_line_parses_controller_format_from_aseqdump(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    payload = recorder._parse_midi_line("16:0   Control change         15, controller 0, value 1")

    assert payload["source"] == "16:0"
    assert payload["event_type"] == "control_change"
    assert payload["channel"] == 15
    assert payload["control"] == 0
    assert payload["value"] == 1
    assert payload["raw"] == "16:0   Control change         15, controller 0, value 1"


def test_parse_midi_line_parses_controller_format_with_extra_spacing(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    payload = recorder._parse_midi_line("16:0 Control change      15,  controller 0,  value 0")

    assert payload["source"] == "16:0"
    assert payload["event_type"] == "control_change"
    assert payload["channel"] == 15
    assert payload["control"] == 0
    assert payload["value"] == 0


def test_parse_midi_line_keeps_unparsed_raw(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    payload = recorder._parse_midi_line("garbled midi line")

    assert payload["raw"] == "garbled midi line"
    assert payload["source"] == "16:0"
    assert "channel" not in payload


def test_recording_file_reports_midi_sidecar(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-06_01-00-00.wav"
    wav.write_bytes(b"wav")
    sidecar = tmp_path / "mix_2026-05-06_01-00-00.midi.jsonl"
    sidecar.write_text("{}\n")
    onair = tmp_path / "mix_2026-05-06_01-00-00.onair.jsonl"
    onair.write_text("{}\n")

    record = recorder.recent_recordings()[0]

    assert record.midi_log_available is True
    assert record.midi_download_url == f"/api/recordings/{wav.name}/midi-download"
    assert record.onair_log_available is True
    assert record.onair_download_url == f"/api/recordings/{wav.name}/onair-download"


def test_start_writes_onair_log_with_seeded_channels(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:00+00:00", "control": 2, "value": 64})

    started = recorder.start()

    onair_path = tmp_path / f"{started.current_filename[:-4]}.onair.jsonl"
    lines = [json.loads(line) for line in onair_path.read_text().splitlines()]

    assert lines[0] == {
        "type": "midi_logging_started",
        "recording_filename": started.current_filename,
        "threshold": 30,
        "time_seconds": 0.0,
    }
    assert lines[1] == {
        "type": "channel_in",
        "channel": 3,
        "value": 64,
        "time_seconds": 0.0,
    }


def test_onair_log_writes_threshold_crossings_once(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    started = recorder.start()
    onair_path = tmp_path / f"{started.current_filename[:-4]}.onair.jsonl"

    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:00+00:00", "control": 0, "value": 31})
    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:01+00:00", "control": 0, "value": 80})
    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:02+00:00", "control": 0, "value": 29})

    lines = [json.loads(line) for line in onair_path.read_text().splitlines()]

    assert [line["type"] for line in lines] == ["midi_logging_started", "channel_in", "channel_out"]
    assert lines[1]["channel"] == 1
    assert lines[1]["value"] == 31
    assert lines[2]["channel"] == 1
    assert lines[2]["value"] == 29


def test_stop_writes_onair_log_stopped_event(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    started = recorder.start()
    onair_path = tmp_path / f"{started.current_filename[:-4]}.onair.jsonl"

    recorder.stop()

    lines = [json.loads(line) for line in onair_path.read_text().splitlines()]
    assert lines[-1]["type"] == "midi_logging_stopped"
    assert isinstance(lines[-1]["time_seconds"], float)


def test_onair_log_uses_updated_threshold_value(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    def fake_run(*args, **kwargs):
        if args[0] == ["aseqdump", "-l"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=" Port    Client name                      Port name\n 16:0    XONE:96                         MIDI\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="card 2: XONE96 [XONE 96 USB 2], device 0: USB Audio [USB Audio]\n",
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder.apply_settings(
        midi_port="16:0",
        input_device="plughw:2,0",
        onair_threshold=44,
        prolink_onair_enabled=True,
        prolink_onair_threshold=1,
        prolink_onair_channel_to_player={"2": 2, "3": 3},
        default_mix_prefix="mix",
        track_id_merge_gap_seconds=10,
        auto_enable_metering=False,
        theme="dark",
        confirm_delete_recordings=True,
        stop_discard_countdown_seconds=3,
    )

    started = recorder.start()
    onair_path = tmp_path / f"{started.current_filename[:-4]}.onair.jsonl"
    first_line = json.loads(onair_path.read_text().splitlines()[0])

    assert first_line["threshold"] == 44


def test_daemon_changes_while_idle_do_not_create_onair_file(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:00+00:00", "control": 1, "value": 64})

    assert list(tmp_path.glob("*.onair.jsonl")) == []


def test_track_ids_export_merges_short_cut_reentry(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-12_22-48-36.wav"
    wav.write_bytes(b"wav")
    onair = tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl"
    onair.write_text(
        "\n".join(
            [
                '{"type":"midi_logging_started","recording_filename":"mix_2026-05-12_22-48-36.wav","threshold":30,"time_seconds":0.0}',
                '{"type":"channel_in","channel":3,"value":127,"time_seconds":0.0}',
                '{"type":"channel_out","channel":3,"value":29,"time_seconds":3.2}',
                '{"type":"channel_in","channel":3,"value":31,"time_seconds":3.3}',
                '{"type":"midi_logging_stopped","time_seconds":20.0}',
            ]
        )
        + "\n"
    )

    _, payload = recorder.track_ids_export_for_recording(wav.name)
    items = json.loads(payload)

    assert items == [
        {
            "title": "CDJ 2",
            "artist": "",
            "links": {},
            "start": "0:00",
            "end": "0:20",
        }
    ]


def test_track_ids_export_splits_long_gap(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-12_22-48-36.wav"
    wav.write_bytes(b"wav")
    onair = tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl"
    onair.write_text(
        "\n".join(
            [
                '{"type":"midi_logging_started","recording_filename":"mix_2026-05-12_22-48-36.wav","threshold":30,"time_seconds":0.0}',
                '{"type":"channel_in","channel":2,"value":45,"time_seconds":10.0}',
                '{"type":"channel_out","channel":2,"value":24,"time_seconds":20.0}',
                '{"type":"channel_in","channel":2,"value":39,"time_seconds":31.5}',
                '{"type":"midi_logging_stopped","time_seconds":50.0}',
            ]
        )
        + "\n"
    )

    _, payload = recorder.track_ids_export_for_recording(wav.name)
    items = json.loads(payload)

    assert items == [
        {
            "title": "CDJ 1",
            "artist": "",
            "links": {},
            "start": "0:10",
            "end": "0:20",
        },
        {
            "title": "CDJ 1",
            "artist": "",
            "links": {},
            "start": "0:31",
            "end": "0:50",
        },
    ]


def test_track_ids_export_uses_configured_merge_gap(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "midi_port": "16:0",
                "midi_port_name_hint": "XONE:96",
                "input_device": "plughw:X2,0",
                "track_id_merge_gap_seconds": 3,
            }
        )
    )
    recorder = Recorder(tmp_path, config_path=config_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-12_22-48-36.wav"
    wav.write_bytes(b"wav")
    onair = tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl"
    onair.write_text(
        "\n".join(
            [
                '{"type":"midi_logging_started","recording_filename":"mix_2026-05-12_22-48-36.wav","threshold":30,"time_seconds":0.0}',
                '{"type":"channel_in","channel":3,"value":127,"time_seconds":0.0}',
                '{"type":"channel_out","channel":3,"value":29,"time_seconds":3.0}',
                '{"type":"channel_in","channel":3,"value":31,"time_seconds":7.5}',
                '{"type":"midi_logging_stopped","time_seconds":20.0}',
            ]
        )
        + "\n"
    )

    _, payload = recorder.track_ids_export_for_recording(wav.name)
    items = json.loads(payload)

    assert len(items) == 2


def test_track_ids_export_handles_overlap_and_stop_close(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-12_22-48-36.wav"
    wav.write_bytes(b"wav")
    onair = tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl"
    onair.write_text(
        "\n".join(
            [
                '{"type":"midi_logging_started","recording_filename":"mix_2026-05-12_22-48-36.wav","threshold":30,"time_seconds":0.0}',
                '{"type":"channel_in","channel":1,"value":35,"time_seconds":0.0}',
                '{"type":"channel_in","channel":3,"value":40,"time_seconds":30.0}',
                '{"type":"channel_out","channel":1,"value":12,"time_seconds":40.0}',
                '{"type":"midi_logging_stopped","time_seconds":60.0}',
            ]
        )
        + "\n"
    )

    _, payload = recorder.track_ids_export_for_recording(wav.name)
    items = json.loads(payload)

    assert items == [
        {
            "title": "Turntable 1",
            "artist": "",
            "links": {},
            "start": "0:00",
            "end": "0:40",
        },
        {
            "title": "CDJ 2",
            "artist": "",
            "links": {},
            "start": "0:30",
            "end": "1:00",
        },
    ]


def test_track_ids_export_handles_duplicate_events(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-12_22-48-36.wav"
    wav.write_bytes(b"wav")
    onair = tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl"
    onair.write_text(
        "\n".join(
            [
                '{"type":"midi_logging_started","recording_filename":"mix_2026-05-12_22-48-36.wav","threshold":30,"time_seconds":0.0}',
                '{"type":"channel_in","channel":4,"value":31,"time_seconds":0.0}',
                '{"type":"channel_in","channel":4,"value":90,"time_seconds":1.0}',
                '{"type":"channel_out","channel":4,"value":12,"time_seconds":8.0}',
                '{"type":"channel_out","channel":4,"value":0,"time_seconds":9.0}',
                '{"type":"midi_logging_stopped","time_seconds":20.0}',
            ]
        )
        + "\n"
    )

    _, payload = recorder.track_ids_export_for_recording(wav.name)
    items = json.loads(payload)

    assert items == [
        {
            "title": "Turntable 2",
            "artist": "",
            "links": {},
            "start": "0:00",
            "end": "0:08",
        }
    ]


def test_track_ids_export_rejects_malformed_log(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    wav = tmp_path / "mix_2026-05-12_22-48-36.wav"
    wav.write_bytes(b"wav")
    onair = tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl"
    onair.write_text('{"type":"channel_in","channel":3}\n')

    with pytest.raises(RecorderError):
        recorder.track_ids_export_for_recording(wav.name)


def test_track_ids_export_formats_hours(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    assert recorder._format_track_time(3661.2) == "1:01:01"


def test_midi_daemon_starts_and_stops(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)

    recorder.start_midi_daemon()
    recorder.shutdown()

    daemon_process = next(process for process in processes if process.command == ["aseqdump", "-p", "16:0"])
    assert daemon_process.signals == [signal.SIGINT]


def test_daemon_payload_updates_channel_state(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False, onair_threshold=30)

    recorder._apply_daemon_midi_payload_locked(
        {
            "ts_utc": "2026-05-12T12:00:00+00:00",
            "control": 2,
            "value": 64,
        }
    )

    payload = recorder.midi_state_payload()

    assert payload["channels"]["CH3"]["value"] == 64
    assert payload["channels"]["CH3"]["on_air"] is True


def test_daemon_threshold_boundary(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False, onair_threshold=30)

    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:00+00:00", "control": 0, "value": 29})
    assert recorder.midi_state_payload()["channels"]["CH1"]["on_air"] is False
    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:01+00:00", "control": 0, "value": 30})
    assert recorder.midi_state_payload()["channels"]["CH1"]["on_air"] is True
    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:02+00:00", "control": 0, "value": 31})
    assert recorder.midi_state_payload()["channels"]["CH1"]["on_air"] is True


def test_apply_settings_updates_onair_threshold_immediately(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    def fake_run(*args, **kwargs):
        if args[0] == ["aseqdump", "-l"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=" Port    Client name                      Port name\n 16:0    XONE:96                         MIDI\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="card 2: XONE96 [XONE 96 USB 2], device 0: USB Audio [USB Audio]\n",
            stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder._apply_daemon_midi_payload_locked({"ts_utc": "2026-05-12T12:00:00+00:00", "control": 0, "value": 35})

    payload = recorder.apply_settings(
        midi_port="16:0",
        input_device="plughw:2,0",
        onair_threshold=40,
        prolink_onair_enabled=True,
        prolink_onair_threshold=1,
        prolink_onair_channel_to_player={"2": 2, "3": 3},
        default_mix_prefix="mix",
        track_id_merge_gap_seconds=10,
        auto_enable_metering=False,
        theme="dark",
        confirm_delete_recordings=True,
        stop_discard_countdown_seconds=3,
    )

    assert payload["settings"]["onair_threshold"] == 40
    assert recorder.midi_state_payload()["threshold"] == 40
    assert recorder.midi_state_payload()["channels"]["CH1"]["on_air"] is False


def test_daemon_restarts_when_process_exits(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder.start_midi_daemon()
    daemon_process = next(process for process in processes if process.command == ["aseqdump", "-p", "16:0"])
    daemon_process.returncode = 1

    recorder._ensure_midi_daemon_locked()

    assert len([process for process in processes if process.command == ["aseqdump", "-p", "16:0"]]) == 2


def test_resolve_midi_port_uses_name_hint_when_port_changes(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False, midi_port="16:0", midi_port_name_hint="XONE:96")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=" Port    Client name                      Port name\n 24:0    XONE:96                         MIDI\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert recorder._resolve_midi_port() == "24:0"


def test_resolve_midi_port_uses_normalized_name_hint(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False, midi_port="16:0", midi_port_name_hint="XONE:96")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=" Port    Client name                      Port name\n 24:0    XONE 96 USB 2                    XONE 96 USB 2 XONE 96 2\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert recorder._resolve_midi_port() == "24:0"


def test_daemon_restarts_when_resolved_port_changes(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    monkeypatch.setattr(recorder, "_resolve_midi_port", lambda: "16:0")

    recorder.start_midi_daemon()
    recorder._ensure_midi_daemon_locked("24:0")

    assert len(processes) == 2
    assert processes[0].signals == [signal.SIGINT]
    assert processes[1].command == ["aseqdump", "-p", "24:0"]


def test_daemon_stops_when_port_disappears(monkeypatch, tmp_path):
    process = FakeProcess(["aseqdump", "-p", "16:0"])
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: process)
    recorder = Recorder(tmp_path, device_check_enabled=False)
    monkeypatch.setattr(recorder, "_resolve_midi_port", lambda: "16:0")
    recorder.start_midi_daemon()

    monkeypatch.setattr(recorder, "_resolve_midi_port", lambda: None)
    recorder._ensure_midi_daemon_locked()

    assert process.signals == [signal.SIGINT]
    assert recorder.midi_state_payload()["midi_online"] is False


def test_daemon_reader_clears_stuck_process_when_stream_ends(monkeypatch, tmp_path):
    process = FakeProcess(["aseqdump", "-p", "16:0"])
    process.stdout = iter([])
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder._daemon_midi_process = process
    recorder._daemon_port_in_use = "16:0"
    recorder._midi_online = True

    recorder._read_daemon_midi_stream(process)

    assert process.terminated is True or process.killed is True
    assert recorder.midi_state_payload()["midi_online"] is False
    assert recorder.midi_state_payload()["midi_error"] == "MIDI daemon disconnected; waiting to reconnect."


def test_daemon_marks_device_available_when_midi_is_online_for_passive_status(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stderr="cannot open audio device\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path, device_check_enabled=True)
    recorder.start_midi_daemon()

    available, error = recorder.check_device_available()

    assert available is True
    assert error is None


def test_list_audio_input_devices(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="card 2: XONE96 [XONE 96 USB 2], device 0: USB Audio [USB Audio]\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path, device_check_enabled=False)

    devices = recorder.list_available_audio_devices()

    assert devices == [
        {
            "id": "plughw:2,0",
            "card_index": 2,
            "device_index": 0,
            "card_id": "XONE96",
            "card_name": "XONE 96 USB 2",
            "device_id": "USB Audio",
            "device_name": "USB Audio",
            "label": "plughw:2,0 — XONE 96 USB 2 / USB Audio",
        }
    ]


def test_apply_settings_updates_devices_and_persists(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    def fake_run(*args, **kwargs):
        if args[0] == ["aseqdump", "-l"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=" Port    Client name                      Port name\n 24:0    XONE 96 USB 2                    XONE 96 USB 2 XONE 96 2\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="card 2: XONE96 [XONE 96 USB 2], device 0: USB Audio [USB Audio]\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    config_path = tmp_path / "config.json"
    recorder = Recorder(tmp_path, config_path=config_path, device_check_enabled=False)
    recorder.start_midi_daemon()

    payload = recorder.apply_settings(
        midi_port="24:0",
        input_device="plughw:2,0",
        onair_threshold=45,
        prolink_onair_enabled=False,
        prolink_onair_threshold=2,
        prolink_onair_channel_to_player={"2": 2, "3": 3},
        default_mix_prefix="vinyl",
        track_id_merge_gap_seconds=7,
        auto_enable_metering=True,
        theme="light",
        confirm_delete_recordings=False,
        stop_discard_countdown_seconds=1,
    )

    assert payload["settings"]["midi_port"] == "24:0"
    assert payload["settings"]["input_device"] == "plughw:2,0"
    assert payload["settings"]["onair_threshold"] == 45
    assert payload["settings"]["prolink_onair_enabled"] is False
    assert payload["settings"]["prolink_onair_threshold"] == 2
    assert payload["settings"]["prolink_onair_channel_to_player"] == {"2": 2, "3": 3}
    assert payload["settings"]["default_mix_prefix"] == "vinyl"
    assert payload["settings"]["track_id_merge_gap_seconds"] == 7
    assert payload["settings"]["auto_enable_metering"] is True
    assert payload["settings"]["theme"] == "light"
    assert payload["settings"]["confirm_delete_recordings"] is False
    assert payload["settings"]["stop_discard_countdown_seconds"] == 1
    persisted = json.loads(config_path.read_text())
    assert persisted["midi_port"] == "24:0"
    assert persisted["input_device"] == "plughw:2,0"
    assert persisted["onair_threshold"] == 45
    assert persisted["prolink_onair_enabled"] is False
    assert persisted["prolink_onair_threshold"] == 2
    assert persisted["prolink_onair_channel_to_player"] == {"2": 2, "3": 3}
    assert persisted["default_mix_prefix"] == "vinyl"
    assert persisted["track_id_merge_gap_seconds"] == 7
    midi_processes = [process for process in processes if process.command == ["aseqdump", "-p", "24:0"]]
    assert len(midi_processes) >= 2
    assert midi_processes[0].signals == [signal.SIGINT]


def test_apply_settings_rejects_invalid_prolink_mapping(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        if args[0] == ["aseqdump", "-l"]:
            return subprocess.CompletedProcess(
                args[0],
                0,
                stdout=" Port    Client name                      Port name\n 24:0    XONE 96 USB 2                    XONE 96 USB 2 XONE 96 2\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout="card 2: XONE96 [XONE 96 USB 2], device 0: USB Audio [USB Audio]\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path, device_check_enabled=False)

    with pytest.raises(ValueError):
        recorder.apply_settings(
            midi_port="24:0",
            input_device="plughw:2,0",
            onair_threshold=30,
            prolink_onair_enabled=True,
            prolink_onair_threshold=1,
            prolink_onair_channel_to_player={"2": 0, "3": 3},
            default_mix_prefix="mix",
            track_id_merge_gap_seconds=10,
            auto_enable_metering=False,
            theme="dark",
            confirm_delete_recordings=True,
            stop_discard_countdown_seconds=3,
        )


def test_apply_settings_rejects_changes_while_recording(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder.start()

    with pytest.raises(RecorderError):
        recorder.apply_settings(
            midi_port="24:0",
            input_device="plughw:2,0",
            onair_threshold=30,
            prolink_onair_enabled=True,
            prolink_onair_threshold=1,
            prolink_onair_channel_to_player={"2": 2, "3": 3},
            default_mix_prefix="mix",
            track_id_merge_gap_seconds=10,
            auto_enable_metering=False,
            theme="dark",
            confirm_delete_recordings=True,
            stop_discard_countdown_seconds=3,
        )


def test_settings_payload_reports_missing_saved_devices(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "midi_port": "24:0",
                "midi_port_name_hint": "XONE 96 USB 2 XONE 96 2",
                "input_device": "plughw:2,0",
                "onair_threshold": 52,
                "prolink_onair_enabled": True,
                "prolink_onair_threshold": 1,
                "prolink_onair_channel_to_player": {"2": 2, "3": 3},
                "default_mix_prefix": "vinyl",
                "track_id_merge_gap_seconds": 8,
                "auto_enable_metering": True,
                "theme": "light",
                "confirm_delete_recordings": False,
                "stop_discard_countdown_seconds": 1,
            }
        )
    )

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""))
    recorder = Recorder(tmp_path, config_path=config_path, device_check_enabled=False)

    payload = recorder.settings_payload()

    assert payload["midi_selected_available"] is False
    assert payload["audio_selected_available"] is False
    assert payload["debug"]["selected_midi_device"] == "24:0"
    assert payload["debug"]["onair_threshold"] == 52
    assert payload["debug"]["selected_audio_input"] == "plughw:2,0"


def test_settings_payload_reports_missing_prolink_status_file(tmp_path):
    recorder = Recorder(
        tmp_path,
        prolink_status_path=tmp_path / "missing-prolink-status.json",
        device_check_enabled=False,
    )

    payload = recorder.settings_payload()

    assert payload["debug"]["prolink_onair"]["available"] is False
    assert payload["debug"]["prolink_onair"]["error"] == "Status file not found."


def test_settings_payload_reports_prolink_status_file(tmp_path):
    status_path = tmp_path / "prolink-status.json"
    status_path.write_text(
        json.dumps(
            {
                "online": True,
                "selected_midi_port": "24:0",
                "resolved_midi_port": "24:0",
                "threshold": 1,
                "mapping": {"2": 2, "3": 3},
                "players_on_air": [2],
                "last_values": {"2": 127, "3": 0},
            }
        )
    )
    recorder = Recorder(tmp_path, prolink_status_path=status_path, device_check_enabled=False)

    payload = recorder.settings_payload()

    assert payload["debug"]["prolink_onair"]["available"] is True
    assert payload["debug"]["prolink_onair"]["online"] is True
    assert payload["debug"]["prolink_onair"]["players_on_air"] == [2]


def test_forced_device_check_still_requires_audio_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stderr="cannot open audio device\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    recorder = Recorder(tmp_path, device_check_enabled=True)
    recorder.start_midi_daemon()

    available, error = recorder.check_device_available(force=True)

    assert available is False
    assert error == "cannot open audio device"


def test_meter_payload_is_idle_by_default(tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)

    payload = recorder.meter_payload()

    assert payload == {
        "recording": False,
        "metering": False,
        "channels": {
            "left": {"peak_db": None, "rms_db": None},
            "right": {"peak_db": None, "rms_db": None},
        },
        "updated_at": None,
    }


def test_start_and_stop_metering(monkeypatch, tmp_path):
    processes = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)

    started = recorder.start_metering()
    stopped = recorder.stop_metering()

    assert started.metering_active is True
    assert stopped.metering_active is False
    monitor_process = next(process for process in processes if process.command and process.command[0] == "ffmpeg")
    assert monitor_process.signals == [signal.SIGINT]


def test_stop_metering_during_recording_keeps_recording(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    started = recorder.start()

    status = recorder.stop_metering()

    assert started.recording is True
    assert status.recording is True
    assert status.metering_active is False


def test_start_recording_reenables_metering(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    recorder = Recorder(tmp_path, device_check_enabled=False)
    recorder.start()
    recorder.stop_metering()

    status = recorder.start_metering()

    assert status.recording is True
    assert status.metering_active is True


def test_device_check_reports_available_while_metering(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stderr=""))
    recorder = Recorder(tmp_path, device_check_enabled=True)
    recorder.start_metering()

    available, error = recorder.check_device_available(force=True)

    assert available is True
    assert error is None


def test_start_metering_raises_when_ffmpeg_exits_immediately(monkeypatch, tmp_path):
    def fake_popen(command, **kwargs):
        if command[0] == "aseqdump":
            return FakeProcess(command, **kwargs)
        process = FakeProcess(command, stderr_output="Error opening input files: Input/output error\n", **kwargs)
        process.returncode = 1
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    recorder = Recorder(tmp_path, device_check_enabled=False)

    with pytest.raises(DeviceUnavailableError) as exc_info:
        recorder.start_metering()

    assert "Input/output error" in str(exc_info.value)


def test_astats_parser_extracts_left_and_right_values():
    parser = AstatsParser()

    assert parser.parse_line("[Parsed_astats_1 @ abc] Channel: 1") is None
    assert parser.parse_line("[Parsed_astats_1 @ abc] Peak level dB: -3.2").channels["left"].peak_db == -3.2
    state = parser.parse_line("[Parsed_astats_1 @ abc] RMS level dB: -18.4")
    assert state.channels["left"].rms_db == -18.4
    parser.parse_line("[Parsed_astats_1 @ abc] Channel: 2")
    parser.parse_line("[Parsed_astats_1 @ abc] Peak level dB: -4.1")
    state = parser.parse_line("[Parsed_astats_1 @ abc] RMS level dB: -19.0")

    assert state.recording is True
    assert state.channels["left"].peak_db == -3.2
    assert state.channels["left"].rms_db == -18.4
    assert state.channels["right"].peak_db == -4.1
    assert state.channels["right"].rms_db == -19.0
    assert state.updated_at is not None


def test_astats_parser_handles_inf_and_noise():
    parser = AstatsParser()

    assert parser.parse_line("ffmpeg version something") is None
    parser.parse_line("Channel: 1")
    state = parser.parse_line("Peak level dB: -inf")
    parser.parse_line("not a value")

    assert state.channels["left"].peak_db is None
    assert state.channels["right"].peak_db is None


def test_astats_parser_extracts_metadata_print_values():
    parser = AstatsParser()

    parser.parse_line("[Parsed_ametadata_1] lavfi.astats.1.Peak_level=-2.5")
    parser.parse_line("[Parsed_ametadata_2] lavfi.astats.1.RMS_level=-14.0")
    parser.parse_line("[Parsed_ametadata_3] lavfi.astats.2.Peak_level=-3.5")
    state = parser.parse_line("[Parsed_ametadata_4] lavfi.astats.2.RMS_level=-15.0")

    assert state.channels["left"].peak_db == -2.5
    assert state.channels["left"].rms_db == -14.0
    assert state.channels["right"].peak_db == -3.5
    assert state.channels["right"].rms_db == -15.0


def test_waveform_generation_and_cache_hit(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    filename = "mix_2026-05-06_01-00-00.wav"
    path = tmp_path / filename
    path.write_bytes(b"wav")
    calls = {"extract": 0}

    monkeypatch.setattr(recorder, "_wav_duration_seconds", lambda _: 3600.0)

    def fake_extract(_):
        calls["extract"] += 1
        return [0.0, 0.1, 0.2, 0.4, 0.8]

    monkeypatch.setattr(recorder, "_extract_waveform_rms_db", fake_extract)

    first = recorder.waveform_for_recording(filename)
    second = recorder.waveform_for_recording(filename)

    assert first.sample_count > 0
    assert all(0.0 <= value <= 1.0 for value in first.samples)
    assert first.generated_at is not None
    assert second.samples == first.samples
    assert calls["extract"] == 1
    assert (tmp_path / ".waveforms" / f"{filename}.json").exists()


def test_waveform_cache_invalidates_on_file_change(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    filename = "mix_2026-05-06_01-00-00.wav"
    path = tmp_path / filename
    path.write_bytes(b"wav")
    calls = {"extract": 0}
    monkeypatch.setattr(recorder, "_wav_duration_seconds", lambda _: 600.0)

    def fake_extract(_):
        calls["extract"] += 1
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(recorder, "_extract_waveform_rms_db", fake_extract)
    recorder.waveform_for_recording(filename)
    path.write_bytes(b"wav-changed")
    recorder.waveform_for_recording(filename)

    assert calls["extract"] == 2


def test_waveform_generation_error_for_empty_values(monkeypatch, tmp_path):
    recorder = Recorder(tmp_path, device_check_enabled=False)
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    monkeypatch.setattr(recorder, "_wav_duration_seconds", lambda _: 10.0)
    monkeypatch.setattr(recorder, "_extract_waveform_rms_db", lambda _: [])

    with pytest.raises(WaveformGenerationError):
        recorder.waveform_for_recording(filename)
