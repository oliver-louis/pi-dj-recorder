# pi-dj-recorder

A LAN-first FastAPI web app for recording DJ mixes from an Allen & Heath Xone:96 on a Raspberry Pi.

It records the Xone:96 USB capture stream with FFmpeg, saves stereo 24-bit WAV files, provides browser-based recording control, live metering, waveform playback and scrubbing, and MIDI-derived on-air logging for later tracklist work.

This project is currently tuned for one real-world setup:

- Raspberry Pi 5
- Raspberry Pi OS Lite
- Allen & Heath Xone:96 over USB
- ALSA input `plughw:X2,0`
- stereo master taken from Xone capture channels `c10` / `c11`
- trusted local network, no authentication

## Features

- Start and stop recordings from a browser
- Stop recordings with `SIGINT` so WAV headers finalise cleanly
- Prevent concurrent recordings
- Optional custom mix names with timestamped filenames
- Live stereo peak/RMS meters in the Record view
- Mobile-friendly Record and Recordings tabs
- In-browser playback with waveform-based scrubbing
- Recent recordings list with rename, delete, and download actions
- Raw MIDI sidecar logging during recording (`.midi.jsonl`)
- Compact on-air transition logging during recording (`.onair.jsonl`)
- Track ID template export inferred from on-air logs
- Always-on MIDI daemon for CH1-CH4 on-air indicators on the Record page

## How it works

### Audio recording

The app starts FFmpeg with this command shape:

```bash
ffmpeg -f alsa -channels 12 -sample_rate 48000 -sample_fmt s32 \
  -i plughw:X2,0 \
  -filter_complex "pan=stereo|c0=c10|c1=c11" \
  -c:a pcm_s24le /home/copper/mixes/mix_YYYY-MM-DD_HH-MM-SS.wav
```

Recording is controlled by a FastAPI backend. The frontend is plain HTML/CSS/JS, served directly by FastAPI.

### Metering

Live stereo meters are derived from the same stereo signal path using FFmpeg `astats` output. Meter updates are streamed to the browser over WebSocket.

### MIDI and on-air state

An always-on MIDI daemon reads:

```bash
aseqdump -p 16:0
```

It tracks controller values for CH1-CH4 and marks a channel as on-air when its value is `>= 30` by default.

During recordings the app also writes:

- `recording-name.midi.jsonl` — raw/parsed MIDI events
- `recording-name.onair.jsonl` — compact `channel_in` / `channel_out` events with timestamps relative to recording start

Those compact logs can be converted on demand into placeholder track-ID JSON for later manual cleanup or future metadata enrichment.

## Repo layout

```text
app/
  main.py                FastAPI routes and lifespan
  recorder.py            Compatibility facade import
  services/              Internal backend modules
  static/                Plain HTML/CSS/JS frontend
systemd/
  pi-recorder.service    Example systemd unit
tests/
```

The backend is modular internally, but still exposes a single `Recorder` facade to keep route code simple.

## Quick start on Raspberry Pi OS Lite

Install system packages:

```bash
sudo apt update
sudo apt install -y git ffmpeg python3-venv alsa-utils
```

Clone the repo:

```bash
cd /home/copper
git clone https://github.com/oliver-louis/pi-dj-recorder.git
cd pi-dj-recorder
```

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create the recordings directory:

```bash
mkdir -p /home/copper/mixes
sudo chown -R copper:copper /home/copper/mixes /home/copper/pi-dj-recorder
```

Run the app manually:

```bash
. .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://<pi-ip>:8000
```

## Deploy as a service

An example unit file is included at `systemd/pi-recorder.service`.

Install it:

```bash
sudo cp /home/copper/pi-dj-recorder/systemd/pi-recorder.service /etc/systemd/system/pi-recorder.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-recorder.service
```

Useful commands:

```bash
systemctl status pi-recorder.service
journalctl -u pi-recorder.service -f
sudo systemctl restart pi-recorder.service
```

If you clone the repo into a different directory, update `WorkingDirectory`, `PATH`, and `ExecStart` in the service file.

## Configuration

Environment variables:

- `PI_RECORDER_RECORDINGS_DIR`  
  default: `/home/copper/mixes`
- `PI_RECORDER_MIDI_PORT`  
  default: `16:0`
- `PI_RECORDER_ONAIR_THRESHOLD`  
  default: `30`

Example local run with a temporary recordings directory:

```bash
PI_RECORDER_RECORDINGS_DIR=/tmp/pi-dj-recorder-mixes uvicorn app.main:app --reload
```

## API overview

Main routes:

- `GET /` — Record page
- `GET /recordings` — Recordings page
- `GET /api/status`
- `POST /api/recordings/start`
- `POST /api/recordings/stop`
- `POST /api/recordings/stop-discard`
- `POST /api/metering/start`
- `POST /api/metering/stop`
- `GET /api/recordings`
- `PATCH /api/recordings/{filename}`
- `DELETE /api/recordings/{filename}`
- `GET /api/recordings/{filename}/download`
- `GET /api/recordings/{filename}/play`
- `GET /api/recordings/{filename}/midi-download`
- `GET /api/recordings/{filename}/onair-download`
- `GET /api/recordings/{filename}/track-ids-export`
- `GET /api/recordings/{filename}/waveform`
- `GET /ws/meters`
- `GET /ws/midi-state`

## Development

Install dev dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

Run tests:

```bash
pytest
```

Run locally:

```bash
uvicorn app.main:app --reload
```

## Known limitations

- Intended for a trusted LAN only; there is currently no authentication.
- The app is tuned for a Xone:96 and a specific channel mapping, not for general audio interface discovery.
- Mixer/audio readiness can sometimes be a little fussy if the app starts while the mixer is already in an odd USB state; a mixer power cycle generally clears it.
- The track-ID export is a template generator, not a finished metadata pipeline.

## Roadmap ideas

- Pro DJ Link / Beat Link metadata support
- Richer track-ID export and metadata enrichment
- Nextcloud / WebDAV sync
- Multi-source or configurable routing support
- Authentication for non-trusted networks

## License

No license has been added yet. If you plan to make this public for reuse, add one before publishing.
