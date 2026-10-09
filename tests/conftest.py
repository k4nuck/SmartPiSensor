import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Hardware/network libs are absent off-Pi; stub before main/smartsensor import them.
for name in ("board", "adafruit_dht", "paho", "paho.mqtt", "paho.mqtt.client"):
	sys.modules.setdefault(name, mock.MagicMock())
sys.modules["board"].D4 = "D4"
sys.modules["paho"].mqtt = sys.modules["paho.mqtt"]
sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]
