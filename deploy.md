# Deploying SmartPiSensor on a Pi

The code is identical on every Pi; only `config.ini` differs.

1. Check out the repo (e.g. `~/projects/dht22`) and install deps (`adafruit-circuitpython-dht`, `paho-mqtt<2`) into the Pi's virtualenv.
   Neither Pi has paho in system python, so the unit below must run the venv's interpreter.
   Find it with `pipenv --venv` (append `/bin/python`). Known paths:
   - Bedroom: `$(pipenv --venv)/bin/python` (pipenv is `/home/k4nuck/.local/bin/pipenv`, not on systemd's PATH, so always use the resolved absolute path in the unit)
   - Crawlspace: `/home/k4nuck/.local/share/virtualenvs/dht22-zI4XIqDa/bin/python`
2. `cp config.ini.example config.ini` and set the Pi's values (the example lists today's bedroom and crawlspace values).
   Set all four HA identity keys (`device_id`, `device_name`, `temp_unique_id`, `hum_unique_id`) on every Pi other than the bedroom:
   they default to the bedroom's, so omitting them makes the Pi overwrite the bedroom's HA entities. Use a distinct `client_id` per Pi too (a shared one makes the broker kick one client off).
   Env vars `SMARTSENSOR_<KEY>` override the file; `SMARTSENSOR_CONFIG` points at a config elsewhere.
3. Install the unit as `/etc/systemd/system/SmartPiSensor.service` (crawlspace: keep its existing unit name, `SmartPiSensorCrawlspace.service`):

```ini
[Unit]
Description=SmartPiSensor
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
User=k4nuck
WorkingDirectory=/home/k4nuck/projects/dht22
ExecStart=<VENV>/bin/python main.py
Restart=always
RestartSec=10
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

   Replace `<VENV>/bin/python` with the absolute interpreter from step 1 (systemd does not expand `$(...)`).

   `User=`: bedroom uses `k4nuck`. The crawlspace service runs as root today and its `dht22.log*` and `temp.fifo` are root-owned,
   so `User=k4nuck` there gives `PermissionError` and an exit-1 restart loop. Either set `User=root` on the crawlspace unit, or first run
   `sudo chown k4nuck:k4nuck /home/k4nuck/projects/dht22/dht22.log* /home/k4nuck/projects/dht22/temp.fifo` and confirm `id k4nuck` includes `gpio`
   (it does today) before switching to `User=k4nuck`.

4. `sudo systemctl daemon-reload && sudo systemctl enable --now SmartPiSensor`.

On the crawlspace Pi the existing `wait-for-broker.conf` drop-in can stay; the program now also retries the broker connection itself.

## Behaviour to rely on
- A dead worker/child or unusable Manager makes the process exit 1, so `Restart=always` restarts it after `RestartSec`.
- SIGTERM stops the children and exits 0 within a few seconds.
- A bad or missing config exits non-zero immediately (see `journalctl -u SmartPiSensor`).

## Verifying
`sudo systemctl stop SmartPiSensor` should return in seconds. `sudo kill -9 $(pgrep -f 'main.py' | tail -1)` (a child) should be followed by a restart within ~15 s.

# ecobee reader (crawlspace Pi only)

`ecobee_service.py` is a separate service from the DHT22 sensor. It pairs with the upstate ecobees as a HomeKit controller
and publishes retained JSON to the home broker (the same `broker_host`/`broker_port` as `config.ini`; no new settings):

- `upstate/ecobee/<device_id>/state`: `{"name","temperature_f","humidity","hvac_mode","hvac_action","target_heat_f","target_cool_f","sensors":[{"name","temperature_f","occupancy"}],"ts"}`, null where a field isn't exposed. Every 60 s.
- `upstate/ecobee/<device_id>/availability`: `online` / `offline`. `offline` is the broker's last-will, and is also published after 2 failed polls in a row (~2 min) and on clean shutdown.

`<device_id>` is the name given to `ecobee_pair.py` (lower-cased, e.g. `living-room`). `name` in the payload is that name title-cased ("Living Room").
The poller only holds a read-only facade; writes happen only in `ecobee_control.py`, in answer to commands (below).

## Install
1. Own venv (do not touch the dht22 one): `python3 -m venv ~/ecobee-venv && ~/ecobee-venv/bin/pip install -r requirements-ecobee.txt`.
   aiohomekit pulls `cryptography`; on a Pi 3 let it use piwheels wheels rather than compiling (slow, can exhaust 1 GB RAM).
2. Same `config.ini` as the sensor service (`broker_host`, `broker_port`, `client_id`, `log_path` are read; `log_path`'s directory also holds `ecobee/pairings.json`, mode 600, dir 700).
   MQTT client ids are `<client_id>-ecobee-<name>`, distinct from the sensor's.
3. Unit `/etc/systemd/system/ecobee-reader.service`:

```ini
[Unit]
Description=ecobee HomeKit reader
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
User=k4nuck
WorkingDirectory=/home/k4nuck/projects/dht22
ExecStart=/home/k4nuck/ecobee-venv/bin/python ecobee_service.py
Restart=always
RestartSec=30
TimeoutStopSec=15
# Keeps zigbee2mqtt from being squeezed on the 1 GB Pi 3. Measured ~44 MB RSS with one thermostat + one room sensor.
MemoryMax=120M

[Install]
WantedBy=multi-user.target
```

   The service exits 1 (and systemd retries every 30 s) until at least one ecobee is paired.

## Pair on site (needs the Pi on the `gergy_upstate` Wi-Fi)
Put the ecobee in HomeKit setup (its screen shows the code), then:

`cd /home/k4nuck/projects/dht22 && ~/ecobee-venv/bin/python ecobee_pair.py living-room 123-45-678`

Repeat for the Backroom (`backroom`). The tool picks the one unpaired thermostat it can see; if it sees several it refuses and asks for `--device-id`.
A wrong code prints `ecobee-pair FAILED: ... wrong setup code?` and exits 1, storing nothing. Then `sudo systemctl enable --now ecobee-reader` (or `restart`).
The tool checks the key directory is writable *before* pairing. If saving still fails afterwards it says `PAIRED, but saving ... failed` and leaves the keys in a mode-600 `ecobee-pairing-<name>.recovery.json` (or prints them as a last resort): merge that into `pairings.json`, do not re-pair.
On stop the service cancels any in-flight read, publishes `offline` and disconnects, well inside `TimeoutStopSec=15`. While the broker is unreachable only the latest state is kept and sent on reconnect.
An ecobee can only be paired once without resetting it; the keys in `ecobee/pairings.json` are the only copy, so back that file up.

## Bench test at home (fake thermostat)
On gergy-voice (192.168.1.207): `pip install -r requirements-dev.txt`, then `python tools/fake_homekit_thermostat.py` (setup code `123-45-678`, port 51826; a thermostat plus one room sensor).
On the Pi (or any host on that LAN) run `ecobee_pair.py fake 123-45-678` then the service. Stopping the fake flips `availability` to `offline` after ~2 min; restarting it flips back to `online`.
Watch with `mosquitto_sub -t 'upstate/#' -v`.

## Commands (gergy-incidents#1878)
The service subscribes (QoS 1, non-retained) to `upstate/ecobee/<device_id>/set` and answers on `upstate/ecobee/<device_id>/result` (QoS 1, non-retained).
Commands: `set_mode`, `set_setpoint`, `turn_off`, `turn_on`, `set_fan`, `set_preset`, `resume_schedule`; envelope `{"id","command","parameters","ts"}`, temperatures in F.
Limits enforced on the Pi: heat targets 45-78 F, cool 65-90 F, auto deadband 3 F (the ecobee exposes none over HomeKit). Window: `ts`+20 s, `ts` at most 5 s in the future. A repeated `id` within 10 minutes returns the cached result and never writes again.
Watch with `mosquitto_sub -t 'upstate/ecobee/+/result' -v`.

Deploying this change on gergy-upstate: `cd ~/projects/dht22 && git pull && sudo systemctl restart ecobee-reader` (no new dependencies, no config change).
`set_fan` writes the aiohomekit-documented speed (100 = on, 0 = auto). **Before trusting `set_preset` on the real thermostats**, confirm the hold-schedule value encoding (`PRESET_VALUES` in `ecobee_control.py`) on site; it was written without ecobee hardware.
Timing: HA waits 25 s for `/result`. Discovery, pre-read, writes and readbacks share one 15 s budget from receipt, the `/result` is published first, and only then is the state refreshed.

# SHIELD exit-node switch (gergy-upstate Pi, gergy-incidents#1893)

`shield_service.py` turns the upstate SHIELD's Tailscale exit node on/off over adb so the in-laws' streaming apps route through ha-main.
It needs `adb` on the Pi (`apt install adb`), already authorized to the SHIELD, and only paho (the sensor's venv works; no aiohomekit).
Optional `config.ini` keys, defaults shown: `shield_slug = upstate_tv`, `shield_adb = 192.168.1.62:5555`, `shield_exit_node = ha-main-remote`. MQTT client id is `<client_id>-shield-<slug>`.

- `upstate/shield/<slug>/set` in (QoS 1, not retained): `{"id","command":"turn_on"|"turn_off","ts"}`. Same 20 s window / 5 s skew / 10 min id cache as the ecobee commands.
- `upstate/shield/<slug>/result` out: `{"id","outcome":"ok|rejected|failed|unknown","error","exit_node"}`. `ok` only after `ip route get 1.1.1.1` on the SHIELD shows (or stops showing) a `tun*` device, polled up to 10 s.
- `upstate/shield/<slug>/state` retained: `{"exit_node": true|false|null, "online", "ts"}`. Route checked every 15 s, published on change and at least every 3 min. `null` = adb unreachable or route unreadable.
- `upstate/shield/<slug>/availability`: `online` while adb answers, else `offline`; also the LWT.

Unit `/etc/systemd/system/shield-exit-node.service` (same shape as the ecobee one, `ExecStart=<venv>/bin/python shield_service.py`, `Restart=always`, `RestartSec=30`, `TimeoutStopSec=15`).
The service starts with no network or SHIELD: MQTT retries on its own and each adb call reconnects once before reporting offline.

**Unverified:** "off" is the same broadcast with an empty `exitNode`. Confirm on site that this clears the exit node on Tailscale 1.104.6 (the `ip route get` check will report `failed` if it does not).

# SHIELD tailnet forwarder (gergy-upstate Pi, gergy-incidents#1896)

`tcp_forwarder.py` lets ha-main reach the upstate SHIELD's Android TV Remote (6466 pairing, 6467 control) and adb (5555) through this Pi. The SHIELD's own tailnet address does not answer those ports; the Pi reaches it on the in-laws' LAN.
Stdlib only, so any python3 works (no venv). Nothing about the SHIELD is hard-coded: `--bind`, `--target`, `--ports` (or `TCP_FORWARDER_BIND` / `_TARGET` / `_PORTS`; ports default `6466,6467,5555`).
It listens **only** on the address given by `--bind`, which must be a tailnet address (100.64.0.0/10); `0.0.0.0`, a LAN IP or a hostname is refused (exit 2), so the in-laws' LAN never sees the listeners. All ports bind or none do.

Unit `/etc/systemd/system/shield-forwarder.service`:

```ini
[Unit]
Description=SHIELD tailnet TCP forwarder
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
User=k4nuck
WorkingDirectory=/home/k4nuck/projects/dht22
ExecStart=/usr/bin/python3 tcp_forwarder.py --bind 100.123.50.43 --target 192.168.1.62
Restart=always
RestartSec=10
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
```

Deploy: `cd ~/projects/dht22 && git pull`, install the unit, `sudo systemctl daemon-reload && sudo systemctl enable --now shield-forwarder`.
Verify on the Pi: `ss -ltn | grep -E ':(6466|6467|5555)\b'` shows only `100.123.50.43:<port>`, never `0.0.0.0` or `*`. From ha-main: `nc -vz 100.123.50.43 6466` and `6467` connect.
If the tailnet address is not up yet at boot the service exits 1 and systemd retries every 10 s. A powered-off SHIELD only closes that one client connection. This does not touch `shield_service.py` or the exit-node topics.
