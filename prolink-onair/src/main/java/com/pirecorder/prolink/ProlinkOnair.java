package com.pirecorder.prolink;

import org.deepsymmetry.beatlink.VirtualCdj;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
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

    private final Config config;
    private final Set<Integer> playersOnAir = new TreeSet<>();
    private final Map<Integer, Integer> lastValuesByChannel = new HashMap<>();
    private Process midiProcess;

    private ProlinkOnair(Config config) {
        this.config = config;
    }

    public static void main(String[] args) throws Exception {
        Config config = Config.fromEnvironment();
        new ProlinkOnair(config).run();
    }

    private void run() throws Exception {
        String resolvedPort = resolveMidiPort();
        System.out.printf(
                "Starting prolink-onair at %s. MIDI port=%s, threshold=%d, mapping=%s%n",
                Instant.now(), resolvedPort, config.threshold, config.channelToPlayer);

        VirtualCdj virtualCdj = VirtualCdj.getInstance();
        virtualCdj.setDeviceName(config.virtualCdjName);
        virtualCdj.start();
        System.out.printf(
                "Virtual CDJ online as player %d using %s -> %s%n",
                virtualCdj.getDeviceNumber(),
                virtualCdj.getLocalAddress().getHostAddress(),
                virtualCdj.getBroadcastAddress().getHostAddress());

        ScheduledExecutorService repeater = Executors.newSingleThreadScheduledExecutor();
        repeater.scheduleAtFixedRate(() -> sendOnAir(virtualCdj), 0, config.repeatMillis, TimeUnit.MILLISECONDS);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            repeater.shutdownNow();
            if (midiProcess != null) {
                midiProcess.destroy();
            }
            try {
                virtualCdj.sendOnAirCommand(Collections.emptySet());
            } catch (Exception ignored) {
                // Best-effort cleanup while the JVM is exiting.
            }
            virtualCdj.stop();
        }));

        runMidiLoop(resolvedPort, virtualCdj);
    }

    private String resolveMidiPort() throws IOException, InterruptedException {
        Process process = new ProcessBuilder(config.midiCaptureBin, "-l")
                .redirectErrorStream(true)
                .start();
        String fallback = config.midiPort;
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                Matcher match = ASEQDUMP_LIST_PORT.matcher(line);
                if (!match.matches()) {
                    continue;
                }
                String port = match.group(1);
                String haystack = (match.group(2) + " " + match.group(3)).toLowerCase();
                if (port.equals(config.midiPort)) {
                    fallback = port;
                }
                if (!config.midiPortNameHint.isEmpty() && haystack.contains(config.midiPortNameHint.toLowerCase())) {
                    return port;
                }
            }
        }
        process.waitFor(3, TimeUnit.SECONDS);
        return fallback;
    }

    private void runMidiLoop(String midiPort, VirtualCdj virtualCdj) throws IOException {
        ProcessBuilder builder = new ProcessBuilder(config.midiCaptureBin, "-p", midiPort);
        builder.redirectErrorStream(true);
        midiProcess = builder.start();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(midiProcess.getInputStream(), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                Optional<MidiControl> event = parseMidiControl(line);
                if (event.isEmpty()) {
                    continue;
                }
                handleMidiControl(event.get(), virtualCdj);
            }
        }
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

    private void handleMidiControl(MidiControl control, VirtualCdj virtualCdj) {
        int mixerChannel = control.controller + 1;
        Integer player = config.channelToPlayer.get(mixerChannel);
        if (player == null) {
            return;
        }

        int clampedValue = Math.max(0, Math.min(127, control.value));
        Integer previous = lastValuesByChannel.put(mixerChannel, clampedValue);
        if (previous != null && previous == clampedValue) {
            return;
        }

        boolean onAir = clampedValue >= config.threshold;
        boolean changed = onAir ? playersOnAir.add(player) : playersOnAir.remove(player);
        if (!changed) {
            return;
        }

        System.out.printf("CH%d -> player %d %s (value=%d)%n", mixerChannel, player, onAir ? "ON" : "OFF", clampedValue);
        sendOnAir(virtualCdj);
    }

    private void sendOnAir(VirtualCdj virtualCdj) {
        try {
            virtualCdj.sendOnAirCommand(new HashSet<>(playersOnAir));
        } catch (Exception exc) {
            System.err.printf("Could not send on-air state %s: %s%n", playersOnAir, exc.getMessage());
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

    private static final class Config {
        final String midiCaptureBin;
        final String midiPort;
        final String midiPortNameHint;
        final int threshold;
        final long repeatMillis;
        final String virtualCdjName;
        final Map<Integer, Integer> channelToPlayer;

        private Config(
                String midiCaptureBin,
                String midiPort,
                String midiPortNameHint,
                int threshold,
                long repeatMillis,
                String virtualCdjName,
                Map<Integer, Integer> channelToPlayer) {
            this.midiCaptureBin = midiCaptureBin;
            this.midiPort = midiPort;
            this.midiPortNameHint = midiPortNameHint;
            this.threshold = threshold;
            this.repeatMillis = repeatMillis;
            this.virtualCdjName = virtualCdjName;
            this.channelToPlayer = channelToPlayer;
        }

        static Config fromEnvironment() {
            return new Config(
                    env("PROLINK_ONAIR_MIDI_CAPTURE_BIN", "aseqdump"),
                    env("PROLINK_ONAIR_MIDI_PORT", "16:0"),
                    env("PROLINK_ONAIR_MIDI_PORT_HINT", "XONE:96"),
                    intEnv("PROLINK_ONAIR_THRESHOLD", 1),
                    longEnv("PROLINK_ONAIR_REPEAT_MS", 1000),
                    env("PROLINK_ONAIR_VIRTUAL_CDJ_NAME", "PI ONAIR"),
                    parseMapping(env("PROLINK_ONAIR_CHANNEL_TO_PLAYER", "2:1,3:2")));
        }

        private static String env(String name, String defaultValue) {
            String value = System.getenv(name);
            if (value == null || value.trim().isEmpty()) {
                return defaultValue;
            }
            return value.trim();
        }

        private static int intEnv(String name, int defaultValue) {
            try {
                return Integer.parseInt(env(name, Integer.toString(defaultValue)));
            } catch (NumberFormatException exc) {
                return defaultValue;
            }
        }

        private static long longEnv(String name, long defaultValue) {
            try {
                return Long.parseLong(env(name, Long.toString(defaultValue)));
            } catch (NumberFormatException exc) {
                return defaultValue;
            }
        }

        private static Map<Integer, Integer> parseMapping(String raw) {
            Map<Integer, Integer> mapping = new HashMap<>();
            for (String entry : raw.split(",")) {
                String[] parts = entry.trim().split(":");
                if (parts.length != 2) {
                    continue;
                }
                try {
                    mapping.put(Integer.parseInt(parts[0].trim()), Integer.parseInt(parts[1].trim()));
                } catch (NumberFormatException ignored) {
                    // Ignore malformed entries; the resulting mapping is printed at startup.
                }
            }
            return Collections.unmodifiableMap(mapping);
        }
    }
}
