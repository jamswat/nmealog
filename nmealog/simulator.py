"""NMEA 0183 TCP simulator that behaves like a terminal server: one line per sentence, many clients."""
from __future__ import annotations

import functools
import math
import operator
import random
import socket
import threading
import time
from datetime import datetime, timezone


def sentence(body: str) -> str:
    cs = functools.reduce(operator.xor, map(ord, body), 0)
    return f"${body}*{cs:02X}\r\n"


def _lat(lat: float) -> tuple[str, str]:
    hemi = "N" if lat >= 0 else "S"
    a = abs(lat)
    d = int(a)
    return f"{d:02d}{(a - d) * 60:07.4f}", hemi


def _lon(lon: float) -> tuple[str, str]:
    hemi = "E" if lon >= 0 else "W"
    a = abs(lon)
    d = int(a)
    return f"{d:03d}{(a - d) * 60:07.4f}", hemi


class Vessel:
    """Dead-reckoned vessel with a slowly changing course."""

    def __init__(self, lat: float = 50.775, lon: float = -1.295, speed_kn: float = 8.0,
                 course: float = 90.0, rng: random.Random | None = None):
        self.lat, self.lon = lat, lon
        self.speed_kn = speed_kn
        self.course = course
        self.rng = rng or random.Random()
        self.t = 0.0

    def step(self, dt: float) -> None:
        self.t += dt
        self.course = (self.course + self.rng.uniform(-0.5, 0.5)) % 360
        dist_nm = self.speed_kn * dt / 3600.0
        rad = math.radians(self.course)
        self.lat += dist_nm * math.cos(rad) / 60.0
        self.lon += dist_nm * math.sin(rad) / (60.0 * max(0.1, math.cos(math.radians(self.lat))))


class Simulator:
    """Produces the sentences for one second of a fictional voyage."""

    def __init__(self, seed: int | None = None, bad_rate: float = 0.002,
                 no_fix_every: int = 900, no_fix_for: int = 30, start: float | None = None):
        self.rng = random.Random(seed)
        self.vessel = Vessel(rng=self.rng)
        self.bad_rate = bad_rate
        self.no_fix_every = no_fix_every
        self.no_fix_for = no_fix_for
        self.tick = 0
        self.clock = start if start is not None else time.time()

    def _no_fix(self) -> bool:
        return self.no_fix_every > 0 and (self.tick % self.no_fix_every) < self.no_fix_for

    def step(self, dt: float = 1.0) -> list[str]:
        self.tick += 1
        self.clock += dt
        self.vessel.step(dt)
        now = datetime.fromtimestamp(self.clock, timezone.utc)
        hhmmss = now.strftime("%H%M%S.00")
        date = now.strftime("%d%m%y")
        lat, ns = _lat(self.vessel.lat)
        lon, ew = _lon(self.vessel.lon)
        nofix = self._no_fix()
        q = "0" if nofix else "1"
        st = "V" if nofix else "A"
        sog = f"{self.vessel.speed_kn:05.1f}"
        cog = f"{self.vessel.course:05.1f}"
        out: list[str] = []
        out.append(sentence(f"GPGGA,{hhmmss},{lat},{ns},{lon},{ew},{q},08,0.9,31.5,M,46.9,M,,"))
        out.append(sentence(f"GPRMC,{hhmmss},{st},{lat},{ns},{lon},{ew},{sog},{cog},{date},003.1,W,A"))
        out.append(sentence(f"GPGLL,{lat},{ns},{lon},{ew},{hhmmss},{st},A"))
        out.append(sentence(f"GPVTG,{cog},T,{cog},M,{sog},N,{self.vessel.speed_kn * 1.852:05.1f},K,A"))
        if self.tick % 5 == 0:
            out.append(sentence("GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.1"))
        if self.tick % 10 == 0:
            out.append(sentence(f"GPZDA,{hhmmss},{now:%d},{now:%m},{now:%Y},00,00"))
        out.append(sentence(f"HEHDT,{self.vessel.course:.1f},T"))
        if self.tick % 2 == 0:
            out.append(sentence(f"HEROT,{self.rng.uniform(-2, 2):.1f},A"))
            out.append(sentence(f"SDDBT,{12.4 + self.rng.uniform(-0.2, 0.2):.1f},f,3.8,M,2.0,F"))
            out.append(sentence(f"WIMWV,{self.rng.uniform(180, 200):.1f},R,8.7,N,A"))
        if self.tick % 15 == 0:
            out.append("!AIVDM,1,1,,A,15M67FC000G?ufbE`FepT@3n00Sa,0*5C\r\n")
        if self.rng.random() < self.bad_rate:
            good = out[0]
            out.append(good[:-4] + "00\r\n")  # wrong checksum
        return out


def serve(listener: socket.socket, sim: Simulator, rate: float = 1.0,
          stop: threading.Event | None = None, drop_after: float = 0.0,
          counters: dict | None = None) -> None:
    """Accept clients and push simulated sentences to every connected client.

    rate: simulated seconds per wall second. drop_after: if > 0, disconnect each
    client after that many wall seconds (to exercise reconnect logic).
    """
    stop = stop or threading.Event()
    counters = counters if counters is not None else {}
    counters.setdefault("connections", 0)
    listener.settimeout(0.05)
    clients: dict[socket.socket, float] = {}
    interval = 1.0 / rate
    next_tick = time.monotonic()
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
            conn.settimeout(1.0)
            clients[conn] = time.monotonic()
            counters["connections"] += 1
        except TimeoutError:
            pass
        now = time.monotonic()
        if drop_after > 0:
            for conn, since in list(clients.items()):
                if now - since >= drop_after:
                    conn.close()
                    del clients[conn]
        if now >= next_tick:
            next_tick += interval
            lines = sim.step()
            payload = "".join(lines).encode("ascii")
            for conn in list(clients):
                try:
                    conn.sendall(payload)
                except OSError:
                    conn.close()
                    del clients[conn]
    for conn in clients:
        conn.close()
