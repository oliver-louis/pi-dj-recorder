const messageEl = document.getElementById("message");
const METER_FLOOR_DB = -60;
let waveformObserver = null;
let activePlayerAudio = null;
let dashboardMeteringActive = false;

function setupThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;

  const applyTheme = (theme) => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
    toggle.textContent = theme === "dark" ? "Light" : "Dark";
    toggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
    redrawLoadedWaveforms();
  };

  applyTheme(localStorage.getItem("theme") || "dark");
  toggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
}

function setupDefaultMixPlaceholder() {
  const input = document.getElementById("mix-name");
  if (!input) return;

  const update = () => {
    if (document.activeElement !== input || input.value.trim() === "") {
      input.placeholder = defaultRecordingFilename();
    }
  };

  update();
  window.setInterval(update, 1000);
}

function defaultRecordingFilename() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return `mix_${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}.wav`;
}

function setMessage(text, isError = false) {
  if (!messageEl) return;
  messageEl.textContent = text || "";
  messageEl.style.color = isError ? "#b82d3a" : "#647184";
}

function formatDuration(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours, minutes, secs].map((value) => String(value).padStart(2, "0")).join(":");
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = units.shift();
  while (size >= 1024 && units.length > 0) {
    size /= 1024;
    unit = units.shift();
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed with ${response.status}`);
  }
  return data;
}

function updateDashboard(status) {
  const stateDot = document.getElementById("state-dot");
  const state = document.getElementById("recording-state");
  const filename = document.getElementById("current-filename");
  const device = document.getElementById("device-state");
  const pid = document.getElementById("process-pid");
  const elapsed = document.getElementById("elapsed-time");
  const size = document.getElementById("file-size");
  const startButton = document.getElementById("start-button");
  const stopButton = document.getElementById("stop-button");
  const stopDiscardButton = document.getElementById("stop-discard-button");
  const meteringToggleButton = document.getElementById("metering-toggle-button");
  const deviceMessage = document.getElementById("device-message");

  stateDot.classList.toggle("recording", status.recording);
  stateDot.classList.toggle("idle", !status.recording);
  state.textContent = status.recording ? "Recording" : "Idle";
  device.textContent = status.device_available ? "Available" : "Unavailable";
  device.title = status.device_error || "";
  deviceMessage.textContent = status.device_available ? "" : "device not online";
  deviceMessage.classList.toggle("visible", !status.device_available);
  filename.textContent = status.current_filename || "-";
  pid.textContent = status.pid || "-";
  elapsed.textContent = formatDuration(status.elapsed_seconds);
  size.textContent = formatBytes(status.current_file_size);
  startButton.disabled = status.recording || !status.device_available;
  stopButton.disabled = !status.recording;
  if (stopDiscardButton) stopDiscardButton.disabled = !status.recording;
  dashboardMeteringActive = Boolean(status.metering_active);
  if (meteringToggleButton) {
    meteringToggleButton.disabled = !status.device_available && !status.recording;
    meteringToggleButton.textContent = dashboardMeteringActive ? "Meters On" : "Meters Off";
  }
}

function connectMeters() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/meters`);

  socket.addEventListener("message", (event) => {
    updateMeters(JSON.parse(event.data));
  });

  socket.addEventListener("close", () => {
    updateMeters(null);
    window.setTimeout(connectMeters, 1500);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
}

async function refreshMidiState() {
  try {
    updateMidiState(await fetchJson("/api/midi/state"));
  } catch (error) {
    updateMidiState(null);
  }
}

function connectMidiState() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/midi-state`);

  socket.addEventListener("message", (event) => {
    updateMidiState(JSON.parse(event.data));
  });

  socket.addEventListener("close", () => {
    updateMidiState(null);
    window.setTimeout(() => {
      refreshMidiState();
      connectMidiState();
    }, 1500);
  });

  socket.addEventListener("error", () => {
    socket.close();
  });
}

function updateMidiState(payload) {
  const status = document.getElementById("midi-daemon-status");
  if (!status) return;
  const online = Boolean(payload && payload.midi_online);
  status.textContent = online ? "MIDI online" : "MIDI offline";
  status.classList.toggle("online", online);
  status.classList.toggle("offline", !online);

  ["CH1", "CH2", "CH3", "CH4"].forEach((name) => {
    const chip = document.getElementById(`on-air-${name}`);
    if (!chip) return;
    const stateEl = chip.querySelector(".on-air-state");
    const channel = payload && payload.channels ? payload.channels[name] : null;
    const onAir = Boolean(channel && channel.on_air);
    chip.classList.toggle("active", onAir);
    if (stateEl) stateEl.textContent = onAir ? "On" : "Off";
  });
}

function updateMeters(payload) {
  const status = document.getElementById("meter-status");
  if (!payload || (!payload.metering && !payload.recording)) {
    status.textContent = "Idle";
    updateMeterChannel("left", null);
    updateMeterChannel("right", null);
    return;
  }
  if (payload.recording && !payload.metering) {
    status.textContent = "Recording (Meters Off)";
    updateMeterChannel("left", null);
    updateMeterChannel("right", null);
    return;
  }
  if (!payload.recording && !payload.metering) {
    status.textContent = "Idle";
    updateMeterChannel("left", null);
    updateMeterChannel("right", null);
    return;
  }

  status.textContent = payload.recording ? "Recording" : "Monitoring";
  updateMeterChannel("left", payload.channels.left);
  updateMeterChannel("right", payload.channels.right);
}

function updateMeterChannel(name, values) {
  const rms = document.getElementById(`meter-${name}-rms`);
  const peak = document.getElementById(`meter-${name}-peak`);
  const value = document.getElementById(`meter-${name}-value`);
  const peakDb = values ? values.peak_db : null;
  const peakPercent = dbToPercent(peakDb);
  const coverPercent = 100 - peakPercent;

  rms.style.width = `${Math.min(100, Math.max(0, coverPercent))}%`;
  peak.style.left = `${peakPercent}%`;
  value.textContent = peakDb === null || peakDb === undefined ? "-inf" : `${peakDb.toFixed(1)} dB`;
}

function dbToPercent(db) {
  if (db === null || db === undefined || !Number.isFinite(db)) return 0;
  const clamped = Math.min(0, Math.max(METER_FLOOR_DB, db));
  return ((clamped - METER_FLOOR_DB) / Math.abs(METER_FLOOR_DB)) * 100;
}

async function refreshStatus() {
  try {
    const status = await fetchJson("/api/status");
    updateDashboard(status);
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function startRecording() {
  setMessage("Starting recording...");
  try {
    const mixName = document.getElementById("mix-name").value.trim();
    const status = await fetchJson("/api/recordings/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mix_name: mixName || null }),
    });
    updateDashboard(status);
    setMessage(`Recording ${status.current_filename}`);
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function stopRecording() {
  setMessage("Stopping recording...");
  try {
    const status = await fetchJson("/api/recordings/stop", { method: "POST" });
    updateDashboard(status);
    setMessage(status.current_filename ? `Stopped ${status.current_filename}` : "Recording stopped.");
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function stopRecordingDiscard() {
  setMessage("Stopping and deleting recording...");
  try {
    const status = await fetchJson("/api/recordings/stop-discard", { method: "POST" });
    updateDashboard(status);
    setMessage("Recording stopped and discarded.");
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function toggleMetering() {
  const wasActive = dashboardMeteringActive;
  const url = wasActive ? "/api/metering/stop" : "/api/metering/start";
  setMessage(wasActive ? "Stopping live metering..." : "Starting live metering...");
  try {
    const status = await fetchJson(url, { method: "POST" });
    updateDashboard(status);
    setMessage(wasActive ? "Live metering disabled." : "Live metering enabled.");
  } catch (error) {
    setMessage(error.message, true);
  }
}

function renderRecordings(recordings) {
  const list = document.getElementById("recordings-list");
  if (!recordings.length) {
    list.innerHTML = "<p class=\"message\">No recordings yet.</p>";
    return;
  }

  list.replaceChildren(...recordings.map((recording) => {
    const item = document.createElement("article");
    item.className = "recording-item";

    const head = document.createElement("div");
    head.className = "recording-head";

    const name = document.createElement("p");
    name.className = "recording-name";
    name.textContent = recording.name;

    const meta = document.createElement("span");
    meta.className = "recording-meta";
    const modified = new Date(recording.modified_time);
    meta.textContent = `${formatBytes(recording.size)} · ${modified.toLocaleString()}`;

    const downloadMenu = document.createElement("details");
    downloadMenu.className = "download-menu";

    const downloadSummary = document.createElement("summary");
    downloadSummary.className = "download";
    downloadSummary.textContent = "Download";

    const downloadList = document.createElement("div");
    downloadList.className = "download-menu-list";

    const buildDownloadOption = (label, href, filename, enabled = true) => {
      const option = document.createElement("a");
      option.className = "download-menu-item";
      option.textContent = label;
      if (enabled && href) {
        option.href = href;
        option.setAttribute("download", filename);
        option.addEventListener("click", () => {
          downloadMenu.removeAttribute("open");
        });
      } else {
        option.setAttribute("aria-disabled", "true");
      }
      return option;
    };

    downloadList.append(
      buildDownloadOption("WAV", recording.download_url, recording.name),
      buildDownloadOption(
        "ID's",
        recording.track_ids_export_url,
        recording.name.replace(/\.wav$/, ".track-ids.json"),
        Boolean(recording.track_ids_export_url),
      ),
      buildDownloadOption(
        "onair.jsonl",
        recording.onair_download_url,
        recording.name.replace(/\.wav$/, ".onair.jsonl"),
        Boolean(recording.onair_log_available && recording.onair_download_url),
      ),
      buildDownloadOption(
        "midi.jsonl",
        recording.midi_download_url,
        recording.name.replace(/\.wav$/, ".midi.jsonl"),
        Boolean(recording.midi_log_available && recording.midi_download_url),
      ),
    );
    downloadMenu.append(downloadSummary, downloadList);

    const controls = document.createElement("div");
    controls.className = "recording-actions";

    const renameInput = document.createElement("input");
    renameInput.type = "text";
    renameInput.maxLength = 120;
    renameInput.value = recording.name.replace(/_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?\.wav$/, "");

    const rename = document.createElement("button");
    rename.className = "secondary";
    rename.type = "button";
    rename.textContent = "Rename";
    rename.addEventListener("click", () => renameRecording(recording.name, renameInput.value));

    const remove = document.createElement("button");
    remove.className = "danger";
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => deleteRecording(recording.name));

    controls.append(renameInput, rename, remove, downloadMenu);
    head.append(name, meta);
    item.append(head, createPlayer(recording), controls);
    return item;
  }));
}

function createPlayer(recording) {
  const player = document.createElement("div");
  player.className = "player";

  const audio = document.createElement("audio");
  audio.preload = "metadata";
  audio.src = recording.play_url;

  const play = document.createElement("button");
  play.className = "play-button";
  play.type = "button";
  play.textContent = "▶";
  play.setAttribute("aria-label", `Play ${recording.name}`);

  const label = document.createElement("label");
  label.className = "sr-only";
  label.textContent = `Waveform seek for ${recording.name}`;

  const time = document.createElement("span");
  time.className = "player-time";
  time.textContent = "00:00 / 00:00";

  const waveformWrap = document.createElement("div");
  waveformWrap.className = "waveform-wrap";

  const waveformCanvas = document.createElement("canvas");
  waveformCanvas.className = "waveform";
  waveformCanvas.width = 1200;
  waveformCanvas.height = 90;
  waveformCanvas.dataset.filename = recording.name;

  const waveformState = document.createElement("span");
  waveformState.className = "waveform-state";
  waveformState.textContent = "Loading waveform…";

  waveformWrap.append(waveformCanvas, waveformState);

  play.addEventListener("click", async () => {
    if (audio.paused) {
      stopOtherPlayback(audio);
      try {
        await audio.play();
      } catch (error) {
        setMessage(error.message || "Could not play this recording.", true);
      }
    } else {
      audio.pause();
    }
  });

  audio.addEventListener("play", () => {
    activePlayerAudio = audio;
    play.textContent = "Ⅱ";
    play.setAttribute("aria-label", `Pause ${recording.name}`);
  });

  audio.addEventListener("pause", () => {
    if (activePlayerAudio === audio) activePlayerAudio = null;
    play.textContent = "▶";
    play.setAttribute("aria-label", `Play ${recording.name}`);
  });

  audio.addEventListener("loadedmetadata", () => {
    updatePlayerTime(audio, time);
  });

  audio.addEventListener("timeupdate", () => {
    updatePlayerTime(audio, time);
  });

  audio.addEventListener("ended", () => {
    play.textContent = "▶";
    play.setAttribute("aria-label", `Play ${recording.name}`);
    updatePlayerTime(audio, time);
    updateWaveformCursor(waveformCanvas, audio);
  });

  audio.addEventListener("timeupdate", () => {
    updateWaveformCursor(waveformCanvas, audio);
  });

  setupWaveformScrub(waveformCanvas, audio, time);
  enqueueWaveformLoad(waveformCanvas, waveformState);

  player.append(audio, play, waveformWrap, label, time);
  return player;
}

function updatePlayerTime(audio, time) {
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
  time.textContent = `${formatClock(current)} / ${formatClock(duration)}`;
}

function formatClock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function enqueueWaveformLoad(canvas, stateEl) {
  if (!("IntersectionObserver" in window)) {
    loadWaveform(canvas, stateEl);
    return;
  }
  if (!waveformObserver) {
    waveformObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const target = entry.target;
        observer.unobserve(target);
        loadWaveform(target, target.nextElementSibling);
      });
    }, { rootMargin: "200px" });
  }
  waveformObserver.observe(canvas);
}

async function loadWaveform(canvas, stateEl) {
  const filename = canvas.dataset.filename;
  if (!filename) return;
  try {
    const payload = await fetchJson(`/api/recordings/${encodeURIComponent(filename)}/waveform`);
    drawWaveform(canvas, payload.samples || []);
    canvas.dataset.samples = JSON.stringify(payload.samples || []);
    if (stateEl) {
      stateEl.textContent = "";
      stateEl.classList.remove("visible");
    }
  } catch (error) {
    if (stateEl) {
      stateEl.textContent = "Waveform unavailable";
      stateEl.classList.add("visible");
    }
  }
}

function drawWaveform(canvas, samples) {
  const width = canvas.clientWidth || 600;
  const height = canvas.clientHeight || 72;
  const dpr = window.devicePixelRatio || 1;
  const styles = getComputedStyle(document.documentElement);
  const fillColor = styles.getPropertyValue("--waveform-fill").trim() || "rgba(235,241,250,0.92)";
  const backgroundColor = styles.getPropertyValue("--waveform-bg-fill").trim() || "rgba(79,157,232,0.10)";
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = backgroundColor;
  ctx.fillRect(0, 0, width, height);
  if (!samples.length) return;

  const step = samples.length / width;
  ctx.fillStyle = fillColor;
  const centerY = Math.floor(height / 2);
  const maxHalf = Math.max(1, centerY - 1);
  for (let x = 0; x < width; x += 1) {
    const start = Math.floor(x * step);
    const end = Math.max(start + 1, Math.floor((x + 1) * step));
    let peak = 0;
    for (let i = start; i < end && i < samples.length; i += 1) {
      peak = Math.max(peak, Number(samples[i]) || 0);
    }
    const halfBar = Math.max(1, Math.round(peak * maxHalf));
    ctx.fillRect(x, centerY - halfBar, 1, halfBar * 2);
  }
}

function redrawLoadedWaveforms() {
  document.querySelectorAll(".waveform").forEach((canvas) => {
    try {
      const samples = JSON.parse(canvas.dataset.samples || "[]");
      drawWaveform(canvas, Array.isArray(samples) ? samples : []);
    } catch (error) {
      drawWaveform(canvas, []);
    }
  });
}

function setupWaveformScrub(canvas, audio, time) {
  let dragging = false;
  const onPointer = (clientX) => {
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0) return;
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    audio.currentTime = ratio * audio.duration;
    updatePlayerTime(audio, time);
    updateWaveformCursor(canvas, audio);
  };

  canvas.addEventListener("pointerdown", (event) => {
    dragging = true;
    canvas.setPointerCapture(event.pointerId);
    onPointer(event.clientX);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    onPointer(event.clientX);
  });
  const stopDragging = () => {
    dragging = false;
  };
  canvas.addEventListener("pointerup", stopDragging);
  canvas.addEventListener("pointercancel", stopDragging);
}

function stopOtherPlayback(nextAudio) {
  if (!activePlayerAudio || activePlayerAudio === nextAudio) return;
  activePlayerAudio.pause();
}

function setupDiscardModal() {
  const trigger = document.getElementById("stop-discard-button");
  const modal = document.getElementById("discard-modal");
  const cancel = document.getElementById("discard-cancel");
  const confirm = document.getElementById("discard-confirm");
  if (!trigger || !modal || !cancel || !confirm) return;

  let timer = null;
  let seconds = 3;
  const close = () => {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    confirm.disabled = true;
    confirm.textContent = "Confirm (3)";
    if (timer) window.clearInterval(timer);
    timer = null;
  };
  const open = () => {
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    seconds = 3;
    confirm.disabled = true;
    confirm.textContent = `Confirm (${seconds})`;
    timer = window.setInterval(() => {
      seconds -= 1;
      if (seconds <= 0) {
        confirm.disabled = false;
        confirm.textContent = "Confirm";
        window.clearInterval(timer);
        timer = null;
      } else {
        confirm.textContent = `Confirm (${seconds})`;
      }
    }, 1000);
  };

  trigger.addEventListener("click", open);
  cancel.addEventListener("click", close);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) close();
  });
  confirm.addEventListener("click", async () => {
    close();
    await stopRecordingDiscard();
  });
}

function updateWaveformCursor(canvas, audio) {
  if (!Number.isFinite(audio.duration) || audio.duration <= 0) {
    canvas.style.setProperty("--cursor", "0%");
    return;
  }
  const percent = (audio.currentTime / audio.duration) * 100;
  canvas.style.setProperty("--cursor", `${Math.min(100, Math.max(0, percent))}%`);
}

async function loadRecordings() {
  try {
    const data = await fetchJson("/api/recordings");
    renderRecordings(data.recordings || []);
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function renameRecording(filename, mixName) {
  const trimmed = mixName.trim();
  if (!trimmed) {
    setMessage("Enter a mix name before renaming.", true);
    return;
  }
  try {
    await fetchJson(`/api/recordings/${encodeURIComponent(filename)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mix_name: trimmed }),
    });
    setMessage("Recording renamed.");
    await loadRecordings();
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function deleteRecording(filename) {
  if (!window.confirm(`Delete ${filename}?`)) return;
  try {
    const response = await fetch(`/api/recordings/${encodeURIComponent(filename)}`, { method: "DELETE" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Request failed with ${response.status}`);
    }
    setMessage("Recording deleted.");
    await loadRecordings();
  } catch (error) {
    setMessage(error.message, true);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  setupThemeToggle();
  if (page === "dashboard") {
    document.getElementById("start-button").addEventListener("click", startRecording);
    document.getElementById("stop-button").addEventListener("click", stopRecording);
    document.getElementById("metering-toggle-button").addEventListener("click", toggleMetering);
    setupDiscardModal();
    setupDefaultMixPlaceholder();
    refreshStatus();
    refreshMidiState();
    window.setInterval(refreshStatus, 1000);
    connectMeters();
    connectMidiState();
  }
  if (page === "recordings") {
    loadRecordings();
    // TODO: Add Nextcloud sync status/action controls here when that feature exists.
  }
});
