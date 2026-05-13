# Pi Recorder - AI Handoff Summary (No Repository Access Required)

## Purpose
This project is a Raspberry Pi web app for recording DJ mixes from an Allen & Heath Xone:96 mixer over USB.  
It provides browser-based controls for recording, live metering, playback, waveform scrubbing, and file management.

Target use is trusted LAN operation (no authentication in current version).

---

## Environment & Audio Context
- Hardware: Raspberry Pi 5 (4GB), Allen & Heath Xone:96
- OS: Raspberry Pi OS Lite
- Audio input: ALSA `plughw:X2,0`
- Input stream: 12-channel capture at 48kHz, `s32`
- Recorded stereo program: FFmpeg channels `c10/c11` mapped to stereo `L/R`
- Output format: WAV (`pcm_s24le`)
- Recording storage directory: `/home/copper/mixes`

Known-good FFmpeg recording base:
```bash
ffmpeg -f alsa -channels 12 -sample_rate 48000 -sample_fmt s32 \
  -i plughw:X2,0 \
  -filter_complex "pan=stereo|c0=c10|c1=c11" \
  -c:a pcm_s24le /home/copper/mixes/mix_YYYY-MM-DD_HH-MM-SS.wav
```

---

## Current Product Features

### 1) Recording Control
- Start recording from browser
- Stop recording from browser
- Stop sends **SIGINT** to FFmpeg so WAV headers finalize correctly
- Prevents second concurrent recording
- Optional “Stop Without Saving”:
  - requires confirmation modal
  - 3-second delay before confirm can be clicked
  - deletes in-progress file after stopping

### 2) File Naming
- Default filename:
  - `mix_YYYY-MM-DD_HH-MM-SS.wav`
- Optional custom mix name:
  - user enters label
  - label is slugified (safe lowercase + hyphens)
  - resulting filename pattern:
    - `custom-name_YYYY-MM-DD_HH-MM-SS.wav`
- Collision-safe suffixing:
  - `-2`, `-3`, etc. if needed

### 3) Recording Status
Status model includes:
- `recording` (bool)
- `metering_active` (bool)
- `current_filename`
- `pid`
- `elapsed_seconds`
- `current_file_size`
- `device_available`
- `device_error`

### 4) Device Availability Handling
- App checks whether mixer/input is available before starting recording
- If unavailable, recording start is blocked with readable error
- UI shows **"device not online"** when unavailable
- Device status is also reflected in mixer status field

### 5) Recordings Management
- Recordings page lists recent WAV files (newest first)
- For each file:
  - Play
  - Download
  - Rename
  - Delete
- Safe filename/path validation to block traversal or unsafe names

### 6) Playback Experience
- Custom per-item player UI
- Waveform acts as seek/scrub control
- Click/tap/drag waveform to seek
- Time display updates during playback
- **Single active playback policy**:
  - if user starts another recording, currently playing one pauses

### 7) Live Metering
- Live stereo metering (L/R peak + RMS dB)
- Meter data streamed to UI via WebSocket
- Metering sourced from FFmpeg `astats` output parsing
- Meter toggle can be used while idle and during recording
- Recording start auto-enables metering
- User can disable metering during recording without stopping recording

---

## API Surface (Conceptual)

### Pages
- `/` -> Record dashboard
- `/recordings` -> Recordings list/player page

### Recording endpoints
- `POST /api/recordings/start`
- `POST /api/recordings/stop`
- `POST /api/recordings/stop-discard`

### Metering
- `POST /api/metering/start`
- `POST /api/metering/stop`
- `GET /ws/meters` (WebSocket stream)

### Status and files
- `GET /api/status`
- `GET /api/recordings`
- `PATCH /api/recordings/{filename}` (rename)
- `DELETE /api/recordings/{filename}`
- `GET /api/recordings/{filename}/download`
- `GET /api/recordings/{filename}/play`
- `GET /api/recordings/{filename}/waveform`

---

## Waveform System (Long-Mix Scrubbing)
- Waveform previews are server-precomputed and cached
- Cache location:
  - `/home/copper/mixes/.waveforms`
- One JSON sidecar per recording
- Cache invalidation uses file fingerprint (size + mtime)
- Waveform generation:
  - uses FFmpeg analysis
  - extracts energy/RMS data
  - normalizes to `0..1`
  - downsamples to compact bins suitable for long mixes/mobile
- Frontend lazy-loads waveform when cards become visible

Goal: useful “energy map” for navigation, not sample-accurate editing.

---

## UI/UX Direction Already Applied
- Plain HTML/CSS/JS (no React)
- Mobile-friendly layout improvements for recording and recordings pages
- Two-tab nav pattern:
  - `Record`
  - `Recordings`
- Dark mode default with theme toggle
- Meter visuals: fill-based behavior tied to current signal level

---

## Architectural Approach
- Backend: FastAPI
- Recorder logic is separated from route layer (service-style ownership)
- FFmpeg lifecycle managed by recorder service
- Uses robust subprocess handling:
  - argument-list invocation
  - process state tracking
  - stale/dead-process cleanup
  - SIGINT-first shutdown strategy with fallback handling
- Blocking operations are isolated from async request loop

---

## Reliability & Safety Principles in Current Build
- Never allow two active recordings
- Safely finalize WAV headers on normal stop
- Strict filename safety and path checks
- Reject invalid file operations with proper HTTP errors
- Graceful behavior if metering data is absent/malformed
- Preserve stable browser behavior when metering or recording transitions occur

---

## Known Scope Boundaries (Not Implemented Yet)
- No auth/user accounts (LAN trust model only)
- No Nextcloud/WebDAV sync yet
- No multi-format export selection yet
- No multitrack/all-channel metering UI
- No advanced editing/DAW-style waveform zoom tiers

---

## High-Value Future Enhancements (Suggested Priority)
1. Nextcloud/WebDAV sync queue with retries and conflict-safe naming
2. Background waveform pre-generation on new recording completion
3. Better metering calibration/ballistics (hold, decay, peak hold reset)
4. Recording annotations/cues/chapters
5. Retention policies and storage health warnings
6. Optional auth for non-trusted networks
7. Optional dual-write or backup destination recording

---

## Guidance for Any AI Advising This Project
When suggesting changes, prioritize:
- preserving FFmpeg SIGINT-stop correctness
- preserving single active recording invariant
- avoiding ALSA device contention
- keeping mobile usability first-class
- maintaining safe file/path handling
- keeping recorder lifecycle logic centralized in a dedicated service layer

Avoid recommendations that require heavy frontend frameworks unless explicitly requested.
