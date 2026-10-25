"""LAN relay: re-serves one device's NMEA stream over TCP and advertises it as _nmea-0183._tcp.

Clients such as GeoClue (Linux), Gnome Maps and Firefox look up this service over mDNS and read
positions from it. Anyone on the network can read the stream, so the relay is off by default.
"""
from __future__ import annotations

import logging
import queue
import socket
import threading

log = logging.getLogger("nmealog.relay")

SERVICE_TYPE = "_nmea-0183._tcp.local."
QUEUE_SIZE = 2000
SEND_TIMEOUT_S = 0.5
MAX_CLIENTS = 50
# Container and VM bridges are not reachable from other machines on the LAN.
# Also excludes VPN and CNI tunnel interfaces.
VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "lo", "tun", "tap", "wg", "cni-")


class NmeaRelay(threading.Thread):
    def __init__(self, port: int, advertise: bool, instance_name: str, max_clients: int = MAX_CLIENTS):
        super().__init__(name="nmea-relay", daemon=True)
        self.port = port
        self.advertise = advertise
        self.instance_name = instance_name
        self.max_clients = max_clients
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._clients: list[socket.socket] = []
        self._zeroconf = None
        self._service_info = None
        self.clients_connected = 0
        self.sentences_sent = 0
        self.dropped = 0
        self.advertised = False
        self.error: str | None = None
        self._listener: socket.socket | None = None

    def publish(self, line: str) -> None:
        """Queue one sentence for every client. Never blocks the caller."""
        try:
            self._queue.put_nowait((line.rstrip("\r\n") + "\r\n").encode("ascii", errors="replace"))
        except queue.Full:
            self.dropped += 1

    def stop(self) -> None:
        self._stop_event.set()

    def start(self) -> None:
        """Bind the port before returning, so clients can connect as soon as start() completes."""
        try:
            self._listener = socket.create_server(("0.0.0.0", self.port), reuse_port=False)
            self._listener.settimeout(0.1)
        except OSError as exc:
            self.error = f"cannot listen on port {self.port}: {exc}"
            log.error("relay: %s", self.error)
            return
        super().start()

    def run(self) -> None:
        if self._listener is None:
            return
        if self.advertise:
            self._start_advertising()
        log.info("relay: serving on port %s", self.port)
        try:
            while not self._stop_event.is_set():
                self._accept()
                self._send_pending()
        finally:
            self._shutdown()

    def _accept(self) -> None:
        try:
            conn, addr = self._listener.accept()
        except TimeoutError:
            return
        except OSError:
            return
        conn.settimeout(SEND_TIMEOUT_S)
        if len(self._clients) >= self.max_clients:
            conn.close()
            log.warning("relay: rejected client %s (max %d clients)", addr[0], self.max_clients)
            return
        self._clients.append(conn)
        self.clients_connected = len(self._clients)
        log.info("relay: client %s connected", addr[0])

    def _send_pending(self) -> None:
        try:
            line = self._queue.get(timeout=0.2)
        except queue.Empty:
            return
        batch = [line]
        while len(batch) < 100:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        payload = b"".join(batch)
        self.sentences_sent += len(batch)  # count before sending, so clients never see data the count misses
        for conn in list(self._clients):
            try:
                conn.sendall(payload)
            except OSError:
                self._drop(conn)

    def _drop(self, conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass
        if conn in self._clients:
            self._clients.remove(conn)
        self.clients_connected = len(self._clients)

    def _start_advertising(self) -> None:
        try:
            import ifaddr
            from zeroconf import ServiceInfo, Zeroconf

            host = socket.gethostname().split(".")[0] or "nmealog"
            addresses = [socket.inet_aton(ip.ip) for adapter in ifaddr.get_adapters()
                         if not adapter.nice_name.startswith(VIRTUAL_IFACE_PREFIXES)
                         for ip in adapter.ips
                         if ip.is_IPv4 and not ip.ip.startswith("127.")]
            self._zeroconf = Zeroconf()
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                f"{self.instance_name} ({host}).{SERVICE_TYPE}",
                addresses=addresses,
                port=self.port,
                server=f"{host}.local.",
            )
            self._zeroconf.register_service(self._service_info)
            self.advertised = True
            log.info("relay: advertised as %s on %s", self.instance_name, addresses)
        except Exception as exc:  # zeroconf can fail on hosts without multicast
            self.error = f"advertising failed: {exc}"
            log.error("relay: %s", self.error)

    def _shutdown(self) -> None:
        if self._zeroconf is not None:
            try:
                self._zeroconf.unregister_service(self._service_info)
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf = None
        for conn in list(self._clients):
            self._drop(conn)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self.advertised = False
        log.info("relay: stopped")

    def status(self) -> dict:
        return {
            "running": self.is_alive(),
            "port": self.port,
            "advertised": self.advertised,
            "clients": self.clients_connected,
            "sent": self.sentences_sent,
            "dropped": self.dropped,
            "error": self.error,
        }
