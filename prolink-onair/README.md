# prolink-onair

Headless Pro DJ Link bridge for the Pi recorder setup.

This service reads Xone:96 channel fader MIDI with `aseqdump` and sends Pro DJ Link
on-air packets with Deep Symmetry `beat-link`. It also watches CDJ loaded-track
metadata and writes a small live status file plus a metadata JSONL log for Track
ID export autofill. It is intentionally independent of the recorder web app: the
CDJ on-air indicators can keep working even if the recording service is stopped
or restarted.

Default mapping:

- Xone CH2 -> CDJ Player 2
- Xone CH3 -> CDJ Player 3
- fader MIDI value `>= 1` counts as on-air
- virtual CDJ player number `4` is used for metadata requests

## Pi prerequisites

```bash
sudo apt update
sudo apt install -y openjdk-21-jdk-headless maven alsa-utils
```

Make sure the Pro DJ Link interface has a persistent link-local address and no
default route:

```bash
sudo nmcli con add type ethernet ifname eth1 con-name prodjlink ipv4.method manual ipv4.addresses 169.254.100.10/16 ipv4.never-default yes
sudo nmcli con up prodjlink
ip -4 addr show eth1
```

## Build

From the repo root on the Pi:

```bash
cd /home/copper/pi-dj-recorder/prolink-onair
mvn -DskipTests package
```

The runnable jar is:

```text
/home/copper/pi-dj-recorder/prolink-onair/target/prolink-onair-0.1.0.jar
```

## Manual test

Stop the service if it is already running:

```bash
sudo systemctl stop pi-prolink-onair.service
```

Run the bridge in the foreground:

```bash
cd /home/copper/pi-dj-recorder
PROLINK_ONAIR_CHANNEL_TO_PLAYER=2:2,3:3 \
PROLINK_ONAIR_THRESHOLD=1 \
java -jar prolink-onair/target/prolink-onair-0.1.0.jar
```

Move Xone channel 2 and 3 faders. The process should print `CH2 -> player 1 ON`
or similar, and the matching CDJ should show on-air.

If the CDJs do not react, confirm Beat Link traffic is visible from the Pi:

```bash
sudo tcpdump -ni eth1 'udp port 50000 or udp port 50001 or udp port 50002'
```

## Install systemd service

```bash
sudo cp /home/copper/pi-dj-recorder/systemd/pi-prolink-onair.service /etc/systemd/system/pi-prolink-onair.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-prolink-onair.service
sudo journalctl -u pi-prolink-onair.service -f
```

The service uses `Restart=always` so it comes back when the mixer or CDJ network
is unplugged and later returns.

## Configuration

The systemd unit uses environment variables:

- `PROLINK_ONAIR_MIDI_CAPTURE_BIN`: defaults to `aseqdump`
- `PROLINK_ONAIR_CONFIG_PATH`: defaults to `config.json`
- `PROLINK_ONAIR_STATUS_PATH`: defaults to `/tmp/pi-prolink-onair-state.json`
- `PROLINK_METADATA_LOG_PATH`: defaults to `/tmp/pi-prolink-metadata.jsonl`
- `PROLINK_ONAIR_MIDI_PORT`: defaults to `24:0`
- `PROLINK_ONAIR_MIDI_PORT_HINT`: defaults to `XONE:96`
- `PROLINK_ONAIR_THRESHOLD`: defaults to `1`
- `PROLINK_ONAIR_CHANNEL_TO_PLAYER`: defaults to `2:2,3:3`
- `PROLINK_VIRTUAL_PLAYER_NUMBER`: defaults to `4`
- `PROLINK_ONAIR_REPEAT_MS`: defaults to `1000`

When `PROLINK_ONAIR_CONFIG_PATH` points at the recorder app's `config.json`,
the sidecar reads these shared app settings and reloads them while running:

- `midi_port`
- `midi_port_name_hint`
- `prolink_onair_enabled`
- `prolink_onair_threshold`
- `prolink_onair_channel_to_player`
- `prolink_metadata_enabled`
- `prolink_virtual_player_number`

If the CDJ lights flicker because the fader sends tiny non-zero noise at the
bottom, raise `PROLINK_ONAIR_THRESHOLD` to `2`.
