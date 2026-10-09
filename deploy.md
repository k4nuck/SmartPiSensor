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
