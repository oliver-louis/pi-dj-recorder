package com.pirecorder.prolink;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.deepsymmetry.beatlink.VirtualCdj;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
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
        virtualCdj.start();
        System.out.printf(
                "Virtual CDJ online as player %d using %s -> %s%n",
                virtualCdj.getDeviceNumber(),
                virtualCdj.getLocalAddress().getHostAddress(),
                virtualCdj.getBroadcastAddress().getHostAddress());

        ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
        scheduler.scheduleAtFixedRate(() -> reloadConfig(virtualCdj), 1, 1, TimeUnit.SECONDS);
        scheduler.scheduleAtFixedRate(() -> sendOnAir(virtualCdj), 0, config.repeatMillis, TimeUnit.MILLISECONDS);
        scheduler.scheduleAtFixedRate(this::writeStatus, 0, 1, TimeUnit.SECONDS);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            scheduler.shutdownNow();
            stopMidiProcess();
            try {
                virtualCdj.sendOnAirCommand(Collections.emptySet());
            } catch (Exception ignored) {
                // Best-effort cleanup while the JVM is exiting.
            }
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
            sendOnAir(virtualCdj);
            writeStatus();
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
        payload.put("config_path", current.configPath.toString());
        payload.put("config_loaded", Files.isRegularFile(current.configPath));
        synchronized (playersOnAir) {
            payload.put("players_on_air", new ArrayList<>(playersOnAir));
            payload.put("last_values", new HashMap<>(lastValuesByChannel));
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

    private static final class RuntimeConfig {
        final boolean enabled;
        final String midiCaptureBin;
        final String midiPort;
        final String midiPortNameHint;
        final int threshold;
        final long repeatMillis;
        final String virtualCdjName;
        final Map<Integer, Integer> channelToPlayer;
        final Path configPath;
        final Path statusPath;

        private RuntimeConfig(
                boolean enabled,
                String midiCaptureBin,
                String midiPort,
                String midiPortNameHint,
                int threshold,
                long repeatMillis,
                String virtualCdjName,
                Map<Integer, Integer> channelToPlayer,
                Path configPath,
                Path statusPath) {
            this.enabled = enabled;
            this.midiCaptureBin = midiCaptureBin;
            this.midiPort = midiPort;
            this.midiPortNameHint = midiPortNameHint;
            this.threshold = threshold;
            this.repeatMillis = repeatMillis;
            this.virtualCdjName = virtualCdjName;
            this.channelToPlayer = channelToPlayer;
            this.configPath = configPath;
            this.statusPath = statusPath;
        }

        static RuntimeConfig load() {
            Path configPath = Paths.get(env("PROLINK_ONAIR_CONFIG_PATH", "config.json"));
            Path statusPath = Paths.get(env("PROLINK_ONAIR_STATUS_PATH", "/tmp/pi-prolink-onair-state.json"));
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
                    configPath,
                    statusPath);
        }

        boolean equivalentTo(RuntimeConfig other) {
            return enabled == other.enabled
                    && midiPort.equals(other.midiPort)
                    && midiPortNameHint.equals(other.midiPortNameHint)
                    && threshold == other.threshold
                    && channelToPlayer.equals(other.channelToPlayer);
        }

        String summary() {
            return String.format(
                    "enabled=%s, config=%s, status=%s, MIDI=%s, threshold=%d, mapping=%s",
                    enabled, configPath, statusPath, midiPort, threshold, channelToPlayer);
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
