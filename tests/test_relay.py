import socket
import threading
import time

from nmealog.relay import NmeaRelay
from nmealog.simulator import sentence


def _free_port():
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _read_lines(sock, count, timeout=5):
    sock.settimeout(timeout)
    data = b""
    end = time.monotonic() + timeout
    while data.count(b"\n") < count and time.monotonic() < end:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        data += chunk
    return data.decode("ascii").splitlines()


def _wait_for_clients(relay, n, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end and relay.clients_connected < n:
        time.sleep(0.05)
    return relay.clients_connected >= n


def test_relay_fans_out_sentences_to_every_client():
    port = _free_port()
    relay = NmeaRelay(port, advertise=False, instance_name="test")
    relay.start()
    try:
        a = socket.create_connection(("127.0.0.1", port), timeout=5)
        b = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert _wait_for_clients(relay, 2)
        line = sentence("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W").strip()
        relay.publish(line)
        relay.publish(line)
        assert _read_lines(a, 2) == [line, line]
        assert _read_lines(b, 2) == [line, line]
        assert relay.sentences_sent == 2
        a.close()
        b.close()
    finally:
        relay.stop()
        relay.join(timeout=5)
    assert not relay.is_alive()


def test_disconnected_client_is_removed():
    port = _free_port()
    relay = NmeaRelay(port, advertise=False, instance_name="test")
    relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert _wait_for_clients(relay, 1)
        c.close()
        end = time.monotonic() + 5
        while time.monotonic() < end and relay.clients_connected:
            relay.publish("$GPGGA,000000*00")
            time.sleep(0.1)
        assert relay.clients_connected == 0
    finally:
        relay.stop()
        relay.join(timeout=5)


def test_port_in_use_is_reported_not_raised():
    blocker = socket.create_server(("0.0.0.0", 0))
    port = blocker.getsockname()[1]
    relay = NmeaRelay(port, advertise=False, instance_name="test")
    try:
        relay.start()
        assert relay.error and "cannot listen" in relay.error
        assert not relay.is_alive()
    finally:
        blocker.close()


def test_advertising_does_not_break_serving():
    port = _free_port()
    relay = NmeaRelay(port, advertise=True, instance_name="nmealog-test")
    relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert _wait_for_clients(relay, 1)
        relay.publish("$GPGGA,000000*00")
        assert _read_lines(c, 1) == ["$GPGGA,000000*00"]
        # advertised depends on the host's multicast support; either outcome is acceptable,
        # but a failure must be recorded rather than raised.
        assert relay.advertised or relay.error
        c.close()
    finally:
        relay.stop()
        relay.join(timeout=10)
