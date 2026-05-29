# prolink-onair

Headless Pro DJ Link on-air bridge for the Pi recorder setup.

This service reads Xone:96 channel fader MIDI with `aseqdump` and sends Pro DJ Link
on-air packets with Deep Symmetry `beat-link`. It is intentionally independent of
the recorder web app: the CDJ on-air indicators can keep working even if the
recording service is stopped or restarted.

Default mapping:

- Xone CH2 -> CDJ Player 1
- Xone CH3 -> CDJ Player 2
- fader MIDI value `>= 1` counts as on-air

## Pi prerequisites

```bash
sudo apt update
sudo apt install -y openjdk-17-jre-headless maven alsa-utils
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
PROLINK_ONAIR_CHANNEL_TO_PLAYER=2:1,3:2 \
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
- `PROLINK_ONAIR_MIDI_PORT`: defaults to `16:0`
- `PROLINK_ONAIR_MIDI_PORT_HINT`: defaults to `XONE:96`
- `PROLINK_ONAIR_THRESHOLD`: defaults to `1`
- `PROLINK_ONAIR_CHANNEL_TO_PLAYER`: defaults to `2:1,3:2`
- `PROLINK_ONAIR_REPEAT_MS`: defaults to `1000`

If the CDJ lights flicker because the fader sends tiny non-zero noise at the
bottom, raise `PROLINK_ONAIR_THRESHOLD` to `2`.
