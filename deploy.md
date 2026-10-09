# Deploying SmartPiSensor on a Pi

The code is identical on every Pi; only `config.ini` differs.

1. Check out the repo (e.g. `~/projects/dht22`) and install deps (`adafruit-circuitpython-dht`, `paho-mqtt<2`).
2. `cp config.ini.example config.ini` and set the Pi's values (the example lists today's bedroom and crawlspace values).
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
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

4. `sudo systemctl daemon-reload && sudo systemctl enable --now SmartPiSensor`.

On the crawlspace Pi the existing `wait-for-broker.conf` drop-in can stay; the program now also retries the broker connection itself.

## Behaviour to rely on
- A dead worker/child or unusable Manager makes the process exit 1, so `Restart=always` restarts it after `RestartSec`.
- SIGTERM stops the children and exits 0 within a few seconds.
- A bad or missing config exits non-zero immediately (see `journalctl -u SmartPiSensor`).

## Verifying
`sudo systemctl stop SmartPiSensor` should return in seconds. `sudo kill -9 $(pgrep -f 'main.py' | tail -1)` (a child) should be followed by a restart within ~15 s.
