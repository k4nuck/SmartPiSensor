import socket
from unittest import mock
import pytest
from smartsensorToMQTT import connect_with_backoff

def test_retries_dns_failure_with_capped_backoff():
	client = mock.Mock()
	client.connect.side_effect = [socket.gaierror("Name or service not known")] * 4 + [None]
	sleeps = []
	connect_with_backoff(client, "h", 1883, sleep=sleeps.append, max_delay=4)
	assert client.connect.call_count == 5
	assert sleeps == [1, 2, 4, 4]

def test_non_network_errors_still_propagate():
	client = mock.Mock()
	client.connect.side_effect = ValueError("bad")
	with pytest.raises(ValueError):
		connect_with_backoff(client, "h", 1883, sleep=lambda s: None)
