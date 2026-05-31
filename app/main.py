from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.recorder import AlreadyRecordingError, DeviceUnavailableError, NotMeteringError, NotRecordingError, Recorder, RecorderError
from app.recorder import TrackIdExportError, WaveformGenerationError


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
PROLINK_RESTART_COMMAND = os.getenv(
    "PI_RECORDER_PROLINK_RESTART_COMMAND",
    "sudo -n systemctl restart pi-prolink-onair.service",
)
PROLINK_RESTART_TIMEOUT_SECONDS = float(os.getenv("PI_RECORDER_PROLINK_RESTART_TIMEOUT_SECONDS", "8"))

recorder = Recorder(
    Path(os.getenv("PI_RECORDER_RECORDINGS_DIR", "/home/copper/mixes")),
    input_device=os.getenv("PI_RECORDER_INPUT_DEVICE", "plughw:X2,0"),
    midi_port=os.getenv("PI_RECORDER_MIDI_PORT", "16:0"),
    midi_port_name_hint=os.getenv("PI_RECORDER_MIDI_PORT_NAME_HINT", "XONE:96"),
    config_path=Path(os.getenv("PI_RECORDER_CONFIG_PATH", "config.json")),
    prolink_status_path=Path(os.getenv("PI_RECORDER_PROLINK_STATUS_PATH", "/tmp/pi-prolink-onair-state.json")),
    prolink_metadata_log_path=Path(os.getenv("PI_RECORDER_PROLINK_METADATA_LOG_PATH", "/tmp/pi-prolink-metadata.jsonl")),
    onair_threshold=int(os.getenv("PI_RECORDER_ONAIR_THRESHOLD", "30")),
)


class StartRecordingRequest(BaseModel):
    mix_name: str | None = Field(default=None, max_length=120)


class RenameRecordingRequest(BaseModel):
    mix_name: str = Field(min_length=1, max_length=120)


class UpdateSettingsRequest(BaseModel):
    midi_port: str = Field(min_length=1, max_length=120)
    input_device: str = Field(min_length=1, max_length=240)
    onair_threshold: int = Field(ge=0, le=127)
    prolink_onair_enabled: bool = True
    prolink_onair_threshold: int = Field(default=1, ge=0, le=127)
    prolink_onair_channel_to_player: dict[str, int] = Field(default_factory=lambda: {"2": 2, "3": 3})
    prolink_metadata_enabled: bool = True
    prolink_virtual_player_number: int = Field(default=4, ge=1, le=4)
    default_mix_prefix: str = Field(min_length=1, max_length=120)
    track_id_merge_gap_seconds: float = Field(ge=0, le=30)
    auto_enable_metering: bool
    theme: str = Field(pattern="^(dark|light)$")
    confirm_delete_recordings: bool
    stop_discard_countdown_seconds: int = Field(ge=0, le=15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(recorder.ensure_recordings_dir)
    await asyncio.to_thread(recorder.start_midi_daemon)
    yield
    await asyncio.to_thread(recorder.shutdown)


app = FastAPI(title="Pi Xone:96 Recorder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/recordings")
async def recordings_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "recordings.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/api/status")
async def status() -> dict[str, object]:
    return asdict(await asyncio.to_thread(recorder.status))


@app.get("/api/settings")
async def settings() -> dict[str, object]:
    return await asyncio.to_thread(recorder.settings_payload)


@app.put("/api/settings")
async def update_settings(request: UpdateSettingsRequest) -> dict[str, object]:
    try:
        return await asyncio.to_thread(
            recorder.apply_settings,
            midi_port=request.midi_port,
            input_device=request.input_device,
            onair_threshold=request.onair_threshold,
            prolink_onair_enabled=request.prolink_onair_enabled,
            prolink_onair_threshold=request.prolink_onair_threshold,
            prolink_onair_channel_to_player=request.prolink_onair_channel_to_player,
            prolink_metadata_enabled=request.prolink_metadata_enabled,
            prolink_virtual_player_number=request.prolink_virtual_player_number,
            default_mix_prefix=request.default_mix_prefix,
            track_id_merge_gap_seconds=request.track_id_merge_gap_seconds,
            auto_enable_metering=request.auto_enable_metering,
            theme=request.theme,
            confirm_delete_recordings=request.confirm_delete_recordings,
            stop_discard_countdown_seconds=request.stop_discard_countdown_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RecorderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/prolink/restart")
async def restart_prolink() -> dict[str, object]:
    try:
        return await asyncio.to_thread(restart_prolink_service)
    except RecorderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def restart_prolink_service() -> dict[str, object]:
    command = shlex.split(PROLINK_RESTART_COMMAND)
    if not command:
        raise RecorderError("Pro Link restart command is empty.")
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROLINK_RESTART_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RecorderError(f"{command[0]} was not found.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RecorderError("Timed out restarting the Pro Link service.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Restart command failed.").strip()
        raise RecorderError(detail)
    return {"ok": True, "command": command}


@app.websocket("/ws/meters")
async def websocket_meters(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await asyncio.to_thread(recorder.meter_payload))
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return


@app.get("/api/midi/state")
async def midi_state() -> dict[str, object]:
    return await asyncio.to_thread(recorder.midi_state_payload)


@app.websocket("/ws/midi-state")
async def websocket_midi_state(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await asyncio.to_thread(recorder.midi_state_payload))
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return


@app.post("/api/recordings/start")
async def start_recording(request: StartRecordingRequest | None = None) -> dict[str, object]:
    try:
        mix_name = request.mix_name if request else None
        status_data = await asyncio.to_thread(recorder.start, mix_name)
    except AlreadyRecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeviceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return asdict(status_data)


@app.post("/api/recordings/stop")
async def stop_recording() -> dict[str, object]:
    try:
        status_data = await asyncio.to_thread(recorder.stop)
    except NotRecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(status_data)


@app.post("/api/recordings/stop-discard")
async def stop_recording_discard() -> dict[str, object]:
    try:
        status_data = await asyncio.to_thread(recorder.stop, discard=True)
    except NotRecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(status_data)


@app.post("/api/metering/start")
async def start_metering() -> dict[str, object]:
    try:
        status_data = await asyncio.to_thread(recorder.start_metering)
    except (RecorderError, DeviceUnavailableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(status_data)


@app.post("/api/metering/stop")
async def stop_metering() -> dict[str, object]:
    try:
        status_data = await asyncio.to_thread(recorder.stop_metering)
    except NotMeteringError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(status_data)


@app.get("/api/recordings")
async def list_recordings() -> dict[str, list[dict[str, object]]]:
    files = await asyncio.to_thread(recorder.recent_recordings)
    return {"recordings": [asdict(file) for file in files]}


@app.patch("/api/recordings/{filename}")
async def rename_recording(filename: str, request: RenameRecordingRequest) -> dict[str, object]:
    try:
        file = await asyncio.to_thread(recorder.rename_recording, filename, request.mix_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Recording not found.") from exc
    except RecorderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(file)


@app.delete("/api/recordings/{filename}", status_code=204)
async def delete_recording(filename: str) -> None:
    try:
        await asyncio.to_thread(recorder.delete_recording, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Recording not found.") from exc
    except RecorderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/recordings/{filename}/download")
async def download_recording(filename: str) -> FileResponse:
    path = await _safe_recording_path(filename)
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@app.get("/api/recordings/{filename}/play")
async def play_recording(filename: str) -> FileResponse:
    path = await _safe_recording_path(filename)
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/recordings/{filename}/midi-download")
async def download_recording_midi(filename: str) -> FileResponse:
    try:
        path = await asyncio.to_thread(recorder.midi_log_path_for_recording, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="MIDI log not found.") from exc
    return FileResponse(path, media_type="application/jsonl", filename=path.name)


@app.get("/api/recordings/{filename}/onair-download")
async def download_recording_onair(filename: str) -> FileResponse:
    try:
        path = await asyncio.to_thread(recorder.onair_log_path_for_recording, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="On-air log not found.") from exc
    return FileResponse(path, media_type="application/jsonl", filename=path.name)


@app.get("/api/recordings/{filename}/track-ids-export")
async def export_track_ids(filename: str) -> Response:
    try:
        export_name, payload = await asyncio.to_thread(recorder.track_ids_export_for_recording, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="On-air log not found.") from exc
    except TrackIdExportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{export_name}"'},
    )


@app.get("/api/recordings/{filename}/waveform")
async def recording_waveform(filename: str) -> dict[str, object]:
    try:
        waveform = await asyncio.to_thread(recorder.waveform_for_recording, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Recording not found.") from exc
    except WaveformGenerationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return asdict(waveform)


async def _safe_recording_path(filename: str) -> Path:
    try:
        return await asyncio.to_thread(recorder.path_for_recording, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Recording not found.") from exc
