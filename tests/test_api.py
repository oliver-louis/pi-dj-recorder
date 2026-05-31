import subprocess

from fastapi.testclient import TestClient

from app import main
from app.recorder import Recorder, WaveformGenerationError
from tests.test_recorder import FakeProcess


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess(command, **kwargs))
    original = main.recorder
    main.recorder = Recorder(tmp_path, device_check_enabled=False)
    main.recorder.start_midi_daemon()
    client = TestClient(main.app)
    return client, original


def test_status_endpoint(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/status")
    finally:
        main.recorder = original

    assert response.status_code == 200
    assert response.json() == {
        "recording": False,
        "metering_active": False,
        "current_filename": None,
        "pid": None,
        "elapsed_seconds": 0,
        "current_file_size": 0,
        "device_available": True,
        "device_error": None,
        "prolink_metadata": {},
    }


def test_start_and_stop_endpoints(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        start = client.post("/api/recordings/start")
        conflict = client.post("/api/recordings/start")
        stop = client.post("/api/recordings/stop")
    finally:
        main.recorder = original

    assert start.status_code == 200
    assert start.json()["recording"] is True
    assert conflict.status_code == 409
    assert stop.status_code == 200
    assert stop.json()["recording"] is False


def test_stop_discard_endpoint(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        start = client.post("/api/recordings/start")
        stop = client.post("/api/recordings/stop-discard")
    finally:
        main.recorder = original

    assert start.status_code == 200
    assert stop.status_code == 200
    assert stop.json()["recording"] is False


def test_metering_toggle_endpoints(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        started = client.post("/api/metering/start")
        stopped = client.post("/api/metering/stop")
    finally:
        main.recorder = original

    assert started.status_code == 200
    assert started.json()["metering_active"] is True
    assert stopped.status_code == 200
    assert stopped.json()["metering_active"] is False


def test_metering_can_be_disabled_while_recording(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        started = client.post("/api/recordings/start")
        stopped = client.post("/api/metering/stop")
    finally:
        main.recorder = original

    assert started.status_code == 200
    assert stopped.status_code == 200
    assert stopped.json()["recording"] is True
    assert stopped.json()["metering_active"] is False


def test_recordings_list_and_download(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    (tmp_path / "mix_2026-05-06_01-00-00.midi.jsonl").write_text("{}\n")
    (tmp_path / "mix_2026-05-06_01-00-00.onair.jsonl").write_text("{}\n")
    client, original = make_client(tmp_path, monkeypatch)
    try:
        listed = client.get("/api/recordings")
        downloaded = client.get(f"/api/recordings/{filename}/download")
        played = client.get(f"/api/recordings/{filename}/play")
        midi = client.get(f"/api/recordings/{filename}/midi-download")
        onair = client.get(f"/api/recordings/{filename}/onair-download")
    finally:
        main.recorder = original

    assert listed.status_code == 200
    assert listed.json()["recordings"][0]["name"] == filename
    assert listed.json()["recordings"][0]["midi_log_available"] is True
    assert listed.json()["recordings"][0]["onair_log_available"] is True
    assert listed.json()["recordings"][0]["track_ids_export_url"] == f"/api/recordings/{filename}/track-ids-export"
    assert downloaded.status_code == 200
    assert downloaded.content == b"wav"
    assert played.status_code == 200
    assert played.headers["content-type"].startswith("audio/wav")
    assert midi.status_code == 200
    assert midi.content == b"{}\n"
    assert onair.status_code == 200
    assert onair.content == b"{}\n"


def test_start_accepts_custom_name(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.post("/api/recordings/start", json={"mix_name": "Balcony Set"})
    finally:
        main.recorder = original

    assert response.status_code == 200
    assert response.json()["current_filename"].startswith("balcony-set_")


def test_start_reports_unavailable_device(tmp_path, monkeypatch):
    def fake_popen(command, **kwargs):
        process = FakeProcess(command, stderr_output="cannot open audio device\n", **kwargs)
        process.returncode = 1
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    original = main.recorder
    main.recorder = Recorder(tmp_path)
    client = TestClient(main.app)
    try:
        response = client.post("/api/recordings/start")
    finally:
        main.recorder = original

    assert response.status_code == 503
    assert response.json()["detail"] == "cannot open audio device"


def test_metering_start_reports_unavailable_device(tmp_path, monkeypatch):
    def fake_popen(command, **kwargs):
        if command[0] == "aseqdump":
            return FakeProcess(command, **kwargs)
        process = FakeProcess(command, stderr_output="Error opening input files: Input/output error\n", **kwargs)
        process.returncode = 1
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    original = main.recorder
    main.recorder = Recorder(tmp_path)
    client = TestClient(main.app)
    try:
        response = client.post("/api/metering/start")
    finally:
        main.recorder = original

    assert response.status_code == 409
    assert response.json()["detail"] == "Error opening input files: Input/output error"


def test_rename_and_delete_endpoints(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    client, original = make_client(tmp_path, monkeypatch)
    try:
        renamed = client.patch(f"/api/recordings/{filename}", json={"mix_name": "Late Session"})
        deleted = client.delete(f"/api/recordings/{renamed.json()['name']}")
    finally:
        main.recorder = original

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "late-session_2026-05-06_01-00-00.wav"
    assert deleted.status_code == 204
    assert not (tmp_path / renamed.json()["name"]).exists()


def test_invalid_recording_filename_is_rejected(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/recordings/not-a-mix.wav/download")
    finally:
        main.recorder = original

    assert response.status_code == 400


def test_meter_websocket_returns_idle_payload(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        with client.websocket_connect("/ws/meters") as websocket:
            payload = websocket.receive_json()
    finally:
        main.recorder = original

    assert payload == {
        "recording": False,
        "metering": False,
        "channels": {
            "left": {"peak_db": None, "rms_db": None},
            "right": {"peak_db": None, "rms_db": None},
        },
        "updated_at": None,
    }


def test_midi_state_endpoint(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/midi/state")
    finally:
        main.recorder = original

    assert response.status_code == 200
    payload = response.json()
    assert payload["midi_online"] is True
    assert payload["threshold"] == 30
    assert set(payload["channels"].keys()) == {"CH1", "CH2", "CH3", "CH4"}


def test_midi_state_websocket_returns_payload(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        with client.websocket_connect("/ws/midi-state") as websocket:
            payload = websocket.receive_json()
    finally:
        main.recorder = original

    assert payload["midi_online"] is True
    assert set(payload["channels"].keys()) == {"CH1", "CH2", "CH3", "CH4"}


def test_settings_page_and_endpoint(tmp_path, monkeypatch):
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
    client, original = make_client(tmp_path, monkeypatch)
    try:
        page = client.get("/settings")
        response = client.get("/api/settings")
    finally:
        main.recorder = original

    assert page.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["editable"] is True
    assert payload["settings"]["midi_port"] == "16:0"
    assert payload["settings"]["onair_threshold"] == 30
    assert payload["settings"]["prolink_onair_enabled"] is True
    assert payload["settings"]["prolink_onair_threshold"] == 1
    assert payload["settings"]["prolink_onair_channel_to_player"] == {"2": 2, "3": 3}
    assert payload["settings"]["prolink_metadata_enabled"] is True
    assert payload["settings"]["prolink_virtual_player_number"] == 4
    assert payload["settings"]["default_mix_prefix"] == "mix"
    assert payload["settings"]["track_id_merge_gap_seconds"] == 10.0
    assert payload["settings"]["auto_enable_metering"] is False
    assert payload["settings"]["theme"] == "dark"
    assert payload["settings"]["confirm_delete_recordings"] is True
    assert payload["settings"]["stop_discard_countdown_seconds"] == 3
    assert payload["midi_devices"][0]["id"] == "24:0"
    assert payload["audio_devices"][0]["id"] == "plughw:2,0"
    assert payload["debug"]["config_path"]


def test_update_settings_endpoint(tmp_path, monkeypatch):
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
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.put(
            "/api/settings",
            json={
                "midi_port": "24:0",
                "input_device": "plughw:2,0",
                "onair_threshold": 46,
                "prolink_onair_enabled": False,
                "prolink_onair_threshold": 2,
                "prolink_onair_channel_to_player": {"2": 2, "3": 3},
                "prolink_metadata_enabled": False,
                "prolink_virtual_player_number": 4,
                "default_mix_prefix": "vinyl",
                "track_id_merge_gap_seconds": 5,
                "auto_enable_metering": True,
                "theme": "light",
                "confirm_delete_recordings": False,
                "stop_discard_countdown_seconds": 1,
            },
        )
    finally:
        main.recorder = original

    assert response.status_code == 200
    assert response.json()["settings"]["midi_port"] == "24:0"
    assert response.json()["settings"]["input_device"] == "plughw:2,0"
    assert response.json()["settings"]["onair_threshold"] == 46
    assert response.json()["settings"]["prolink_onair_enabled"] is False
    assert response.json()["settings"]["prolink_onair_threshold"] == 2
    assert response.json()["settings"]["prolink_onair_channel_to_player"] == {"2": 2, "3": 3}
    assert response.json()["settings"]["prolink_metadata_enabled"] is False
    assert response.json()["settings"]["prolink_virtual_player_number"] == 4
    assert response.json()["settings"]["default_mix_prefix"] == "vinyl"
    assert response.json()["settings"]["theme"] == "light"


def test_restart_prolink_endpoint_success(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main, "PROLINK_RESTART_COMMAND", "sudo -n systemctl restart pi-prolink-onair.service")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.post("/api/prolink/restart")
    finally:
        main.recorder = original

    assert response.status_code == 200
    assert response.json()["ok"] is True
    restart_call = calls[-1]
    assert restart_call[0] == ["sudo", "-n", "systemctl", "restart", "pi-prolink-onair.service"]
    assert restart_call[1]["timeout"] == main.PROLINK_RESTART_TIMEOUT_SECONDS


def test_restart_prolink_endpoint_reports_failure(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="sudo: a password is required\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.post("/api/prolink/restart")
    finally:
        main.recorder = original

    assert response.status_code == 503
    assert response.json()["detail"] == "sudo: a password is required"


def test_update_settings_rejects_invalid_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""))
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.put(
            "/api/settings",
            json={
                "midi_port": "99:9",
                "input_device": "plughw:9,9",
                "onair_threshold": 30,
                "prolink_onair_enabled": True,
                "prolink_onair_threshold": 1,
                "prolink_onair_channel_to_player": {"2": 2, "3": 3},
                "default_mix_prefix": "mix",
                "track_id_merge_gap_seconds": 10,
                "auto_enable_metering": False,
                "theme": "dark",
                "confirm_delete_recordings": True,
                "stop_discard_countdown_seconds": 3,
            },
        )
    finally:
        main.recorder = original

    assert response.status_code == 400


def test_update_settings_rejects_while_busy(tmp_path, monkeypatch):
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
    client, original = make_client(tmp_path, monkeypatch)
    try:
        client.post("/api/recordings/start")
        response = client.put(
            "/api/settings",
            json={
                "midi_port": "24:0",
                "input_device": "plughw:2,0",
                "onair_threshold": 30,
                "prolink_onair_enabled": True,
                "prolink_onair_threshold": 1,
                "prolink_onair_channel_to_player": {"2": 2, "3": 3},
                "default_mix_prefix": "mix",
                "track_id_merge_gap_seconds": 10,
                "auto_enable_metering": False,
                "theme": "dark",
                "confirm_delete_recordings": True,
                "stop_discard_countdown_seconds": 3,
            },
        )
    finally:
        main.recorder = original

    assert response.status_code == 409


def test_update_settings_rejects_invalid_values(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""))
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.put(
            "/api/settings",
            json={
                "midi_port": "16:0",
                "input_device": "plughw:X2,0",
                "onair_threshold": 128,
                "prolink_onair_enabled": True,
                "prolink_onair_threshold": 128,
                "prolink_onair_channel_to_player": {"2": 2, "3": 3},
                "default_mix_prefix": "",
                "track_id_merge_gap_seconds": 31,
                "auto_enable_metering": False,
                "theme": "violet",
                "confirm_delete_recordings": True,
                "stop_discard_countdown_seconds": 16,
            },
        )
    finally:
        main.recorder = original

    assert response.status_code == 422


def test_midi_state_reflects_updated_threshold(tmp_path, monkeypatch):
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
    client, original = make_client(tmp_path, monkeypatch)
    try:
        updated = client.put(
            "/api/settings",
            json={
                "midi_port": "16:0",
                "input_device": "plughw:2,0",
                "onair_threshold": 41,
                "prolink_onair_enabled": True,
                "prolink_onair_threshold": 1,
                "prolink_onair_channel_to_player": {"2": 2, "3": 3},
                "default_mix_prefix": "mix",
                "track_id_merge_gap_seconds": 10,
                "auto_enable_metering": False,
                "theme": "dark",
                "confirm_delete_recordings": True,
                "stop_discard_countdown_seconds": 3,
            },
        )
        midi = client.get("/api/midi/state")
    finally:
        main.recorder = original

    assert updated.status_code == 200
    assert midi.status_code == 200
    assert midi.json()["threshold"] == 41


def test_waveform_endpoint_success(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    client, original = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main.recorder, "_wav_duration_seconds", lambda _: 120.0)
    monkeypatch.setattr(main.recorder, "_extract_waveform_rms_db", lambda _: [0.1, 0.2, 0.3, 0.4])
    try:
        response = client.get(f"/api/recordings/{filename}/waveform")
    finally:
        main.recorder = original

    assert response.status_code == 200
    payload = response.json()
    assert payload["duration_seconds"] == 120.0
    assert payload["sample_count"] > 0
    assert len(payload["samples"]) == payload["sample_count"]
    assert all(0 <= value <= 1 for value in payload["samples"])
    assert payload["generated_at"] is not None


def test_waveform_endpoint_generation_failure(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    client, original = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main.recorder, "waveform_for_recording", lambda _: (_ for _ in ()).throw(WaveformGenerationError("bad waveform")))
    try:
        response = client.get(f"/api/recordings/{filename}/waveform")
    finally:
        main.recorder = original

    assert response.status_code == 503
    assert response.json()["detail"] == "bad waveform"


def test_waveform_endpoint_rejects_invalid_filename(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/recordings/not-a-mix.wav/waveform")
    finally:
        main.recorder = original

    assert response.status_code == 400


def test_waveform_endpoint_returns_404_for_missing_file(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/recordings/mix_2026-05-06_01-00-00.wav/waveform")
    finally:
        main.recorder = original

    assert response.status_code == 404


def test_midi_download_rejects_invalid_filename(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/recordings/not-a-mix.wav/midi-download")
    finally:
        main.recorder = original

    assert response.status_code == 400


def test_midi_download_returns_404_when_sidecar_missing(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get(f"/api/recordings/{filename}/midi-download")
    finally:
        main.recorder = original

    assert response.status_code == 404
    assert response.json()["detail"] == "MIDI log not found."


def test_onair_download_rejects_invalid_filename(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/recordings/not-a-mix.wav/onair-download")
    finally:
        main.recorder = original

    assert response.status_code == 400


def test_onair_download_returns_404_when_sidecar_missing(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get(f"/api/recordings/{filename}/onair-download")
    finally:
        main.recorder = original

    assert response.status_code == 404
    assert response.json()["detail"] == "On-air log not found."


def test_track_ids_export_download_success(tmp_path, monkeypatch):
    filename = "mix_2026-05-12_22-48-36.wav"
    (tmp_path / filename).write_bytes(b"wav")
    (tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl").write_text(
        "\n".join(
            [
                '{"type":"midi_logging_started","recording_filename":"mix_2026-05-12_22-48-36.wav","threshold":30,"time_seconds":0.0}',
                '{"type":"channel_in","channel":3,"value":127,"time_seconds":0.0}',
                '{"type":"midi_logging_stopped","time_seconds":12.0}',
            ]
        )
        + "\n"
    )
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get(f"/api/recordings/{filename}/track-ids-export")
    finally:
        main.recorder = original

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="mix_2026-05-12_22-48-36.track-ids.json"'
    assert response.json() == [
        {
            "title": "CDJ 2",
            "artist": "",
            "links": {},
            "start": "0:00",
            "end": "0:12",
        }
    ]


def test_track_ids_export_rejects_invalid_filename(tmp_path, monkeypatch):
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get("/api/recordings/not-a-mix.wav/track-ids-export")
    finally:
        main.recorder = original

    assert response.status_code == 400


def test_track_ids_export_returns_404_when_sidecar_missing(tmp_path, monkeypatch):
    filename = "mix_2026-05-06_01-00-00.wav"
    (tmp_path / filename).write_bytes(b"wav")
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get(f"/api/recordings/{filename}/track-ids-export")
    finally:
        main.recorder = original

    assert response.status_code == 404
    assert response.json()["detail"] == "On-air log not found."


def test_track_ids_export_returns_503_for_malformed_log(tmp_path, monkeypatch):
    filename = "mix_2026-05-12_22-48-36.wav"
    (tmp_path / filename).write_bytes(b"wav")
    (tmp_path / "mix_2026-05-12_22-48-36.onair.jsonl").write_text('{"type":"channel_in","channel":3}\n')
    client, original = make_client(tmp_path, monkeypatch)
    try:
        response = client.get(f"/api/recordings/{filename}/track-ids-export")
    finally:
        main.recorder = original

    assert response.status_code == 503
