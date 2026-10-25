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

`ecobee_service.py` is a separate service from the DHT22 sensor. It pairs with the upstate ecobees as a read-only HomeKit controller
and publishes retained JSON to the home broker (the same `broker_host`/`broker_port` as `config.ini`; no new settings):

- `upstate/ecobee/<device_id>/state`: `{"name","temperature_f","humidity","hvac_mode","hvac_action","target_heat_f","target_cool_f","sensors":[{"name","temperature_f","occupancy"}],"ts"}`, null where a field isn't exposed. Every 60 s.
- `upstate/ecobee/<device_id>/availability`: `online` / `offline`. `offline` is the broker's last-will, and is also published after 2 failed polls in a row (~2 min) and on clean shutdown.

`<device_id>` is the name given to `ecobee_pair.py` (lower-cased, e.g. `living-room`). `name` in the payload is that name title-cased ("Living Room").
It never writes to a thermostat: the poller only holds a facade with `get_characteristics` and `list_accessories_and_characteristics`.

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
