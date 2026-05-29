package com.pirecorder.prolink;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.deepsymmetry.beatlink.CdjStatus;
import org.deepsymmetry.beatlink.DeviceUpdate;
import org.deepsymmetry.beatlink.Util;
import org.deepsymmetry.beatlink.VirtualCdj;
import org.deepsymmetry.beatlink.data.SearchableItem;
import org.deepsymmetry.beatlink.data.TrackMetadata;
import org.deepsymmetry.beatlink.data.MetadataFinder;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class ProlinkOnair {
    private static final Pattern ASEQDUMP_LIST_PORT = Pattern.compile("^\\s*(\\d+:\\d+)\\s+(.+?)\\s{2,}(.+?)\\s*$");
    private static final Pattern ASEQDUMP_CONTROLLER_LINE = Pattern.compile(
            "^\\s*(\\d+:\\d+)\\s+(.+?)\\s+(\\d+),\\s*controller\\s+(\\d+),\\s*value\\s+(\\d+)\\s*$",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern ASEQDUMP_CONTROL = Pattern.compile("\\b(?:control|param)=(\\d+)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern ASEQDUMP_VALUE = Pattern.compile("\\bvalue=(\\d+)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern ASEQDUMP_ALT_CONTROL = Pattern.compile("\\bcontroller\\s+(\\d+)\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern ASEQDUMP_ALT_VALUE = Pattern.compile("\\bvalue\\s+(\\d+)\\b", Pattern.CASE_INSENSITIVE);

    private final ObjectMapper mapper = new ObjectMapper();
    private final Set<Integer> playersOnAir = new TreeSet<>();
    private final Map<Integer, Integer> lastValuesByChannel = new HashMap<>();
    private final Map<Integer, Map<String, Object>> playerMetadata = new HashMap<>();
    private final Map<Integer, Map<String, Object>> playerPlayback = new HashMap<>();
    private final Map<Integer, String> playerMetadataSignatures = new HashMap<>();
    private volatile RuntimeConfig config;
    private volatile Process midiProcess;
    private volatile boolean online = false;
    private volatile String error = null;
    private volatile String resolvedMidiPort = null;

    private ProlinkOnair(RuntimeConfig config) {
        this.config = config;
    }

    public static void main(String[] args) throws Exception {
        RuntimeConfig config = RuntimeConfig.load();
        new ProlinkOnair(config).run();
    }

    private void run() throws Exception {
        System.out.printf("Starting prolink-onair at %s. %s%n", Instant.now(), config.summary());

        VirtualCdj virtualCdj = VirtualCdj.getInstance();
        virtualCdj.setDeviceName(config.virtualCdjName);
        virtualCdj.setUseStandardPlayerNumber(true);
        virtualCdj.addUpdateListener(this::handleDeviceUpdate);
        virtualCdj.start((byte) config.virtualPlayerNumber);
        System.out.printf(
                "Virtual CDJ online as player %d using %s -> %s%n",
                virtualCdj.getDeviceNumber(),
                virtualCdj.getLocalAddress().getHostAddress(),
                virtualCdj.getBroadcastAddress().getHostAddress());

        MetadataFinder metadataFinder = MetadataFinder.getInstance();
        metadataFinder.addTrackMetadataListener(this::handleMetadataUpdate);
        metadataFinder.start();

        ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
        scheduler.scheduleAtFixedRate(() -> reloadConfig(virtualCdj), 1, 1, TimeUnit.SECONDS);
        scheduler.scheduleAtFixedRate(() -> sendOnAir(virtualCdj), 0, config.repeatMillis, TimeUnit.MILLISECONDS);
        scheduler.scheduleAtFixedRate(this::writeStatus, 0, 1, TimeUnit.SECONDS);
        scheduler.scheduleAtFixedRate(() -> pollMetadata(metadataFinder), 2, 2, TimeUnit.SECONDS);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            scheduler.shutdownNow();
            stopMidiProcess();
            try {
                virtualCdj.sendOnAirCommand(Collections.emptySet());
            } catch (Exception ignored) {
                // Best-effort cleanup while the JVM is exiting.
            }
            metadataFinder.stop();
            virtualCdj.stop();
        }));

        while (true) {
            RuntimeConfig current = config;
            if (!current.enabled) {
                online = false;
                resolvedMidiPort = null;
                TimeUnit.MILLISECONDS.sleep(500);
                continue;
            }
            runMidiLoop(current, virtualCdj);
            TimeUnit.MILLISECONDS.sleep(500);
        }
    }

    private void reloadConfig(VirtualCdj virtualCdj) {
        RuntimeConfig previous = config;
        RuntimeConfig next = RuntimeConfig.load();
        config = next;
        if (!next.equivalentTo(previous)) {
            System.out.printf("Reloaded prolink-onair config. %s%n", next.summary());
            recomputePlayersOnAir(next);
            recomputeMetadataChannels(next);
            sendOnAir(virtualCdj);
            writeStatus();
        }
        if (next.virtualPlayerNumber != previous.virtualPlayerNumber) {
            System.out.println("Virtual player number changed; exiting so systemd can restart the bridge.");
            System.exit(0);
        }
        if (!next.enabled) {
            synchronized (playersOnAir) {
                playersOnAir.clear();
            }
            sendOnAir(virtualCdj);
            stopMidiProcess();
            online = false;
            error = null;
            resolvedMidiPort = null;
            return;
        }
        if (!next.midiPort.equals(previous.midiPort) || !next.midiPortNameHint.equals(previous.midiPortNameHint)) {
            stopMidiProcess();
        }
    }

    private void pollMetadata(MetadataFinder metadataFinder) {
        RuntimeConfig current = config;
        if (!current.metadataEnabled) {
            synchronized (playerMetadata) {
                playerMetadata.clear();
                playerMetadataSignatures.clear();
            }
            writeStatus();
            return;
        }
        for (Integer player : current.playerToChannel().keySet()) {
            try {
                TrackMetadata metadata = metadataFinder.getLatestMetadataFor(player);
                if (metadata != null) {
                    handleMetadata(player, metadata, current);
                }
            } catch (Exception exc) {
                error = "Could not poll metadata: " + exc.getMessage();
            }
        }
    }

    private void handleMetadataUpdate(Object updateObject) {
        RuntimeConfig current = config;
        if (!current.metadataEnabled) {
            return;
        }
        try {
            int player = (Integer) updateObject.getClass().getField("player").get(updateObject);
            Object metadataObject = updateObject.getClass().getField("metadata").get(updateObject);
            if (metadataObject instanceof TrackMetadata) {
                handleMetadata(player, (TrackMetadata) metadataObject, current);
            } else if (metadataObject == null) {
                synchronized (playerMetadata) {
                    playerMetadata.remove(player);
                    playerMetadataSignatures.remove(player);
                }
                writeStatus();
            }
        } catch (ReflectiveOperationException exc) {
            error = "Could not process metadata update: " + exc.getMessage();
        }
    }

    private void handleMetadata(int player, TrackMetadata metadata, RuntimeConfig current) {
        Integer channel = current.playerToChannel().get(player);
        if (channel == null) {
            return;
        }
        Map<String, Object> payload = metadataPayload(player, channel, metadata);
        String signature = metadataSignature(payload);
        boolean changed;
        synchronized (playerMetadata) {
            changed = !signature.equals(playerMetadataSignatures.get(player));
            playerMetadata.put(player, payload);
            playerMetadataSignatures.put(player, signature);
        }
        if (changed) {
            appendMetadataEvent(payload, current);
            writeStatus();
        }
    }

    private void handleDeviceUpdate(DeviceUpdate update) {
        RuntimeConfig current = config;
        int player = update.getDeviceNumber();
        Integer channel = current.playerToChannel().get(player);
        if (channel == null) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("player", player);
        payload.put("channel", channel);
        payload.put("play_state", playState(update));
        payload.put("playing", playing(update));
        payload.put("is_master", update.isTempoMaster());
        payload.put("synced", update.isSynced());
        payload.put("bpm", normalizedBpm(update));
        payload.put("pitch_percent", Util.pitchToPercentage(update.getPitch()));
        payload.put("updated_at", Instant.now().toString());
        synchronized (playerPlayback) {
            playerPlayback.put(player, payload);
        }
        writeStatus();
    }

    private Map<String, Object> metadataPayload(int player, int channel, TrackMetadata metadata) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("type", "track_loaded");
        payload.put("ts_utc", Instant.now().toString());
        payload.put("loaded_at", Instant.now().toString());
        payload.put("player", player);
        payload.put("channel", channel);
        payload.put("artist", searchableLabel(metadata.getArtist()));
        payload.put("title", stringValue(metadata.getTitle()));
        payload.put("duration_seconds", metadata.getDuration());
        payload.put("bpm", metadata.getTempo() > 0 ? metadata.getTempo() / 100.0 : null);
        payload.put("key", searchableLabel(metadata.getKey()));
        payload.put("album", searchableLabel(metadata.getAlbum()));
        payload.put("label", searchableLabel(metadata.getLabel()));
        if (metadata.trackReference != null) {
            payload.put("source_player", metadata.trackReference.player);
            payload.put("source_slot", metadata.trackReference.slot.toString());
            payload.put("rekordbox_id", metadata.trackReference.rekordboxId);
            payload.put("track_type", metadata.trackReference.trackType.toString());
        }
        return payload;
    }

    private void appendMetadataEvent(Map<String, Object> payload, RuntimeConfig current) {
        try {
            Path parent = current.metadataLogPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            String line = mapper.writeValueAsString(payload) + System.lineSeparator();
            Files.write(
                    current.metadataLogPath,
                    line.getBytes(StandardCharsets.UTF_8),
                    StandardOpenOption.CREATE,
                    StandardOpenOption.APPEND);
        } catch (IOException exc) {
            error = "Could not write metadata log: " + exc.getMessage();
            System.err.println(error);
        }
    }

    private void recomputeMetadataChannels(RuntimeConfig current) {
        synchronized (playerMetadata) {
            Map<Integer, Map<String, Object>> next = new HashMap<>();
            Map<Integer, String> nextSignatures = new HashMap<>();
            for (Map.Entry<Integer, Map<String, Object>> entry : playerMetadata.entrySet()) {
                Integer channel = current.playerToChannel().get(entry.getKey());
                if (channel == null) {
                    continue;
                }
                Map<String, Object> metadata = new LinkedHashMap<>(entry.getValue());
                metadata.put("channel", channel);
                next.put(entry.getKey(), metadata);
                nextSignatures.put(entry.getKey(), metadataSignature(metadata));
            }
            playerMetadata.clear();
            playerMetadata.putAll(next);
            playerMetadataSignatures.clear();
            playerMetadataSignatures.putAll(nextSignatures);
        }
        synchronized (playerPlayback) {
            Map<Integer, Map<String, Object>> next = new HashMap<>();
            for (Map.Entry<Integer, Map<String, Object>> entry : playerPlayback.entrySet()) {
                Integer channel = current.playerToChannel().get(entry.getKey());
                if (channel == null) {
                    continue;
                }
                Map<String, Object> playback = new LinkedHashMap<>(entry.getValue());
                playback.put("channel", channel);
                next.put(entry.getKey(), playback);
            }
            playerPlayback.clear();
            playerPlayback.putAll(next);
        }
    }

    private void runMidiLoop(RuntimeConfig current, VirtualCdj virtualCdj) {
        try {
            String resolvedPort = resolveMidiPort(current);
            resolvedMidiPort = resolvedPort;
            ProcessBuilder builder = new ProcessBuilder(current.midiCaptureBin, "-p", resolvedPort);
            builder.redirectErrorStream(true);
            midiProcess = builder.start();
            online = true;
            error = null;
            writeStatus();

            try (BufferedReader reader = new BufferedReader(new InputStreamReader(midiProcess.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    RuntimeConfig active = config;
                    if (!active.enabled || !active.midiPort.equals(current.midiPort)) {
                        stopMidiProcess();
                        return;
                    }
                    Optional<MidiControl> event = parseMidiControl(line);
                    if (event.isPresent()) {
                        handleMidiControl(event.get(), active, virtualCdj);
                    }
                }
            }
        } catch (Exception exc) {
            online = false;
            error = exc.getMessage();
        } finally {
            online = false;
            midiProcess = null;
            writeStatus();
        }
    }

    private String resolveMidiPort(RuntimeConfig current) throws IOException, InterruptedException {
        Process process = new ProcessBuilder(current.midiCaptureBin, "-l")
                .redirectErrorStream(true)
                .start();
        String fallback = current.midiPort;
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                Matcher match = ASEQDUMP_LIST_PORT.matcher(line);
                if (!match.matches()) {
                    continue;
                }
                String port = match.group(1);
                String haystack = (match.group(2) + " " + match.group(3)).toLowerCase();
                if (port.equals(current.midiPort)) {
                    fallback = port;
                }
                if (!current.midiPortNameHint.isEmpty() && haystack.contains(current.midiPortNameHint.toLowerCase())) {
                    return port;
                }
            }
        }
        process.waitFor(3, TimeUnit.SECONDS);
        return fallback;
    }

    private Optional<MidiControl> parseMidiControl(String line) {
        Matcher controllerLine = ASEQDUMP_CONTROLLER_LINE.matcher(line.trim());
        if (controllerLine.matches()) {
            return Optional.of(new MidiControl(Integer.parseInt(controllerLine.group(4)), Integer.parseInt(controllerLine.group(5))));
        }

        Matcher control = ASEQDUMP_CONTROL.matcher(line);
        Matcher value = ASEQDUMP_VALUE.matcher(line);
        if (!control.find()) {
            control = ASEQDUMP_ALT_CONTROL.matcher(line);
            if (!control.find()) {
                return Optional.empty();
            }
        }
        if (!value.find()) {
            value = ASEQDUMP_ALT_VALUE.matcher(line);
            if (!value.find()) {
                return Optional.empty();
            }
        }
        return Optional.of(new MidiControl(Integer.parseInt(control.group(1)), Integer.parseInt(value.group(1))));
    }

    private void handleMidiControl(MidiControl control, RuntimeConfig active, VirtualCdj virtualCdj) {
        int mixerChannel = control.controller + 1;
        Integer player = active.channelToPlayer.get(mixerChannel);
        if (player == null) {
            return;
        }

        int clampedValue = Math.max(0, Math.min(127, control.value));
        boolean changed;
        synchronized (playersOnAir) {
            lastValuesByChannel.put(mixerChannel, clampedValue);
            boolean onAir = clampedValue >= active.threshold;
            changed = onAir ? playersOnAir.add(player) : playersOnAir.remove(player);
        }
        if (!changed) {
            writeStatus();
            return;
        }

        System.out.printf("CH%d -> player %d %s (value=%d)%n", mixerChannel, player, clampedValue >= active.threshold ? "ON" : "OFF", clampedValue);
        sendOnAir(virtualCdj);
        writeStatus();
    }

    private void recomputePlayersOnAir(RuntimeConfig active) {
        synchronized (playersOnAir) {
            playersOnAir.clear();
            if (!active.enabled) {
                return;
            }
            for (Map.Entry<Integer, Integer> entry : lastValuesByChannel.entrySet()) {
                Integer player = active.channelToPlayer.get(entry.getKey());
                if (player != null && entry.getValue() >= active.threshold) {
                    playersOnAir.add(player);
                }
            }
        }
    }

    private void sendOnAir(VirtualCdj virtualCdj) {
        try {
            Set<Integer> snapshot;
            synchronized (playersOnAir) {
                snapshot = new HashSet<>(playersOnAir);
            }
            virtualCdj.sendOnAirCommand(snapshot);
        } catch (Exception exc) {
            error = "Could not send on-air state: " + exc.getMessage();
            System.err.println(error);
        }
    }

    private void stopMidiProcess() {
        Process process = midiProcess;
        if (process != null) {
            process.destroy();
        }
    }

    private void writeStatus() {
        RuntimeConfig current = config;
        Map<String, Object> payload = new HashMap<>();
        payload.put("online", online);
        payload.put("enabled", current.enabled);
        payload.put("error", error);
        payload.put("selected_midi_port", current.midiPort);
        payload.put("resolved_midi_port", resolvedMidiPort);
        payload.put("threshold", current.threshold);
        payload.put("mapping", current.channelToPlayer);
        payload.put("metadata_enabled", current.metadataEnabled);
        payload.put("virtual_player_number", current.virtualPlayerNumber);
        payload.put("metadata_log_path", current.metadataLogPath.toString());
        payload.put("config_path", current.configPath.toString());
        payload.put("config_loaded", Files.isRegularFile(current.configPath));
        synchronized (playersOnAir) {
            payload.put("players_on_air", new ArrayList<>(playersOnAir));
            payload.put("last_values", new HashMap<>(lastValuesByChannel));
        }
        synchronized (playerMetadata) {
            payload.put("players", mergedPlayerStatus());
        }
        payload.put("updated_at", Instant.now().toString());
        try {
            Path parent = current.statusPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            mapper.writeValue(current.statusPath.toFile(), payload);
        } catch (IOException exc) {
            System.err.printf("Could not write status file %s: %s%n", current.statusPath, exc.getMessage());
        }
    }

    private static final class MidiControl {
        final int controller;
        final int value;

        MidiControl(int controller, int value) {
            this.controller = controller;
            this.value = value;
        }
    }

    private static String searchableLabel(SearchableItem item) {
        if (item == null || item.label == null) {
            return "";
        }
        return item.label;
    }

    private static String stringValue(String value) {
        return value == null ? "" : value;
    }

    private static String metadataSignature(Map<String, Object> payload) {
        return payload.get("player") + ":" + payload.get("channel") + ":" + payload.get("artist") + ":" + payload.get("title") + ":" + payload.get("rekordbox_id");
    }

    private Map<String, Object> mergedPlayerStatus() {
        Map<Integer, Map<String, Object>> merged = new HashMap<>();
        synchronized (playerMetadata) {
            for (Map.Entry<Integer, Map<String, Object>> entry : playerMetadata.entrySet()) {
                merged.put(entry.getKey(), new LinkedHashMap<>(entry.getValue()));
            }
        }
        synchronized (playerPlayback) {
            for (Map.Entry<Integer, Map<String, Object>> entry : playerPlayback.entrySet()) {
                Map<String, Object> player = merged.computeIfAbsent(entry.getKey(), ignored -> new LinkedHashMap<>());
                player.putAll(entry.getValue());
            }
        }
        return new TreeMapStringKeys(merged).asMap();
    }

    private static String playState(DeviceUpdate update) {
        if (update instanceof CdjStatus) {
            String raw = ((CdjStatus) update).getPlayState1().toString();
            if (raw.contains("PLAYING")) {
                return "Playing";
            }
            if (raw.contains("CUE")) {
                return "Cue";
            }
            if (raw.contains("PAUSED")) {
                return "Paused";
            }
            return titleCase(raw);
        }
        return playing(update) ? "Playing" : "Paused";
    }

    private static boolean playing(DeviceUpdate update) {
        if (update instanceof CdjStatus) {
            String raw = ((CdjStatus) update).getPlayState1().toString();
            return raw.contains("PLAYING") || raw.contains("LOOPING");
        }
        return update.getBpm() > 0 && update.getBpm() != 65535;
    }

    private static Double normalizedBpm(DeviceUpdate update) {
        double bpm = update.getEffectiveTempo();
        if (!Double.isFinite(bpm) || bpm <= 0 || update.getBpm() == 65535) {
            return null;
        }
        return Math.round(bpm * 10.0) / 10.0;
    }

    private static String titleCase(String raw) {
        String lower = raw.replace('_', ' ').toLowerCase();
        return lower.isEmpty() ? lower : Character.toUpperCase(lower.charAt(0)) + lower.substring(1);
    }

    private static final class TreeMapStringKeys {
        private final Map<Integer, Map<String, Object>> source;

        TreeMapStringKeys(Map<Integer, Map<String, Object>> source) {
            this.source = source;
        }

        Map<String, Object> asMap() {
            Map<String, Object> result = new LinkedHashMap<>();
            for (Integer key : new TreeSet<>(source.keySet())) {
                result.put(Integer.toString(key), source.get(key));
            }
            return result;
        }
    }

    private static final class RuntimeConfig {
        final boolean enabled;
        final String midiCaptureBin;
        final String midiPort;
        final String midiPortNameHint;
        final int threshold;
        final long repeatMillis;
        final String virtualCdjName;
        final Map<Integer, Integer> channelToPlayer;
        final boolean metadataEnabled;
        final int virtualPlayerNumber;
        final Path configPath;
        final Path statusPath;
        final Path metadataLogPath;

        private RuntimeConfig(
                boolean enabled,
                String midiCaptureBin,
                String midiPort,
                String midiPortNameHint,
                int threshold,
                long repeatMillis,
                String virtualCdjName,
                Map<Integer, Integer> channelToPlayer,
                boolean metadataEnabled,
                int virtualPlayerNumber,
                Path configPath,
                Path statusPath,
                Path metadataLogPath) {
            this.enabled = enabled;
            this.midiCaptureBin = midiCaptureBin;
            this.midiPort = midiPort;
            this.midiPortNameHint = midiPortNameHint;
            this.threshold = threshold;
            this.repeatMillis = repeatMillis;
            this.virtualCdjName = virtualCdjName;
            this.channelToPlayer = channelToPlayer;
            this.metadataEnabled = metadataEnabled;
            this.virtualPlayerNumber = virtualPlayerNumber;
            this.configPath = configPath;
            this.statusPath = statusPath;
            this.metadataLogPath = metadataLogPath;
        }

        static RuntimeConfig load() {
            Path configPath = Paths.get(env("PROLINK_ONAIR_CONFIG_PATH", "config.json"));
            Path statusPath = Paths.get(env("PROLINK_ONAIR_STATUS_PATH", "/tmp/pi-prolink-onair-state.json"));
            Path metadataLogPath = Paths.get(env("PROLINK_METADATA_LOG_PATH", "/tmp/pi-prolink-metadata.jsonl"));
            Map<String, Object> raw = readConfig(configPath);
            return new RuntimeConfig(
                    boolValue(raw.get("prolink_onair_enabled"), true),
                    env("PROLINK_ONAIR_MIDI_CAPTURE_BIN", "aseqdump"),
                    stringValue(raw.get("midi_port"), env("PROLINK_ONAIR_MIDI_PORT", "24:0")),
                    stringValue(raw.get("midi_port_name_hint"), env("PROLINK_ONAIR_MIDI_PORT_HINT", "XONE:96")),
                    intValue(raw.get("prolink_onair_threshold"), intEnv("PROLINK_ONAIR_THRESHOLD", 1)),
                    longEnv("PROLINK_ONAIR_REPEAT_MS", 1000),
                    env("PROLINK_ONAIR_VIRTUAL_CDJ_NAME", "PI ONAIR"),
                    parseMapping(raw.get("prolink_onair_channel_to_player"), env("PROLINK_ONAIR_CHANNEL_TO_PLAYER", "2:2,3:3")),
                    boolValue(raw.get("prolink_metadata_enabled"), true),
                    intInRange(raw.get("prolink_virtual_player_number"), intEnv("PROLINK_VIRTUAL_PLAYER_NUMBER", 4), 1, 4),
                    configPath,
                    statusPath,
                    metadataLogPath);
        }

        boolean equivalentTo(RuntimeConfig other) {
            return enabled == other.enabled
                    && metadataEnabled == other.metadataEnabled
                    && virtualPlayerNumber == other.virtualPlayerNumber
                    && midiPort.equals(other.midiPort)
                    && midiPortNameHint.equals(other.midiPortNameHint)
                    && threshold == other.threshold
                    && channelToPlayer.equals(other.channelToPlayer);
        }

        String summary() {
            return String.format(
                    "enabled=%s, metadata=%s, virtualPlayer=%d, config=%s, status=%s, metadataLog=%s, MIDI=%s, threshold=%d, mapping=%s",
                    enabled, metadataEnabled, virtualPlayerNumber, configPath, statusPath, metadataLogPath, midiPort, threshold, channelToPlayer);
        }

        Map<Integer, Integer> playerToChannel() {
            Map<Integer, Integer> result = new HashMap<>();
            for (Map.Entry<Integer, Integer> entry : channelToPlayer.entrySet()) {
                result.put(entry.getValue(), entry.getKey());
            }
            return result;
        }

        private static Map<String, Object> readConfig(Path path) {
            if (!Files.isRegularFile(path)) {
                return Collections.emptyMap();
            }
            try {
                return new ObjectMapper().readValue(path.toFile(), new TypeReference<Map<String, Object>>() {});
            } catch (IOException exc) {
                System.err.printf("Could not read config file %s: %s%n", path, exc.getMessage());
                return Collections.emptyMap();
            }
        }

        private static Map<Integer, Integer> parseMapping(Object raw, String fallback) {
            Map<Integer, Integer> mapping = new HashMap<>();
            if (raw instanceof Map<?, ?>) {
                Map<?, ?> rawMap = (Map<?, ?>) raw;
                for (Map.Entry<?, ?> entry : rawMap.entrySet()) {
                    Integer key = integerObject(entry.getKey());
                    Integer value = integerObject(entry.getValue());
                    if (key != null && value != null) {
                        mapping.put(key, value);
                    }
                }
            }
            if (mapping.isEmpty()) {
                for (String entry : fallback.split(",")) {
                    String[] parts = entry.trim().split(":");
                    if (parts.length != 2) {
                        continue;
                    }
                    Integer key = integerObject(parts[0].trim());
                    Integer value = integerObject(parts[1].trim());
                    if (key != null && value != null) {
                        mapping.put(key, value);
                    }
                }
            }
            return Collections.unmodifiableMap(mapping);
        }

        private static String env(String name, String defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.trim().isEmpty()) {
                return defaultValue;
            }
            return value.trim();
        }

        private static String stringValue(Object value, String defaultValue) {
            if (value == null || value.toString().trim().isEmpty()) {
                return defaultValue;
            }
            return value.toString().trim();
        }

        private static boolean boolValue(Object value, boolean defaultValue) {
            if (value == null) {
                return defaultValue;
            }
            if (value instanceof Boolean) {
                return (Boolean) value;
            }
            return Boolean.parseBoolean(value.toString());
        }

        private static int intValue(Object value, int defaultValue) {
            Integer parsed = integerObject(value);
            return parsed == null ? defaultValue : Math.max(0, Math.min(127, parsed));
        }

        private static int intInRange(Object value, int defaultValue, int min, int max) {
            Integer parsed = integerObject(value);
            int candidate = parsed == null ? defaultValue : parsed;
            return Math.max(min, Math.min(max, candidate));
        }

        private static int intEnv(String name, int defaultValue) {
            Integer parsed = integerObject(env(name, Integer.toString(defaultValue)));
            return parsed == null ? defaultValue : parsed;
        }

        private static long longEnv(String name, long defaultValue) {
            try {
                return Long.parseLong(env(name, Long.toString(defaultValue)));
            } catch (NumberFormatException exc) {
                return defaultValue;
            }
        }

        private static Integer integerObject(Object value) {
            if (value instanceof Number) {
                return ((Number) value).intValue();
            }
            if (value == null) {
                return null;
            }
            try {
                return Integer.parseInt(value.toString());
            } catch (NumberFormatException exc) {
                return null;
            }
        }
    }
}
