"""Command line entry point: python -m nmealog <command>."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import socket
import sys
import threading
import time
import urllib.request

from .config import load_config


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_collector(args) -> int:
    from .collector import Collector

    stop = threading.Event()

    def handle(signum, _frame):
        logging.getLogger("nmealog").info("signal %s received, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    Collector(load_config(), stop).run()
    return 0


def cmd_simulate(args) -> int:
    from .simulator import Simulator, serve

    listener = socket.create_server(("0.0.0.0", args.port), reuse_port=False)
    logging.getLogger("nmealog.simulator").info("simulator listening on port %s", args.port)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve(listener, Simulator(seed=args.seed), rate=args.rate, stop=stop, drop_after=args.drop_after)
    listener.close()
    return 0


def cmd_hash_password(args) -> int:
    from werkzeug.security import generate_password_hash

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("Use at least 8 characters.", file=sys.stderr)
        return 1
    print(generate_password_hash(password))
    return 0


def cmd_init_db(args) -> int:
    from .db import connect, init_db

    cfg = load_config()
    cfg.ensure_dirs()
    conn = connect(cfg.db_path)
    init_db(conn)
    conn.close()
    print(f"database ready: {cfg.db_path}")
    return 0


def cmd_healthcheck(args) -> int:
    if args.target == "collector":
        from .config import load_config as _load
        from .db import connect, get_meta

        conn = connect(_load().db_path)
        raw = get_meta(conn, "collector_heartbeat")
        conn.close()
        if not raw:
            print("no heartbeat yet", file=sys.stderr)
            return 1
        age = time.time() - json.loads(raw)["ts"]
        if age > 60:
            print(f"heartbeat is {age:.0f}s old", file=sys.stderr)
            return 1
        return 0
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=5) as resp:
            return 0 if resp.status == 200 else 1
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nmealog")
    parser.add_argument("--log-level", default="info")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collector", help="connect to devices and record data")

    sim = sub.add_parser("simulate", help="serve simulated NMEA over TCP for testing")
    sim.add_argument("--port", type=int, default=4001)
    sim.add_argument("--rate", type=float, default=1.0, help="simulated seconds per second")
    sim.add_argument("--seed", type=int, default=None)
    sim.add_argument("--drop-after", type=float, default=0.0,
                     help="disconnect each client after N seconds (tests reconnects)")

    sub.add_parser("hash-password", help="print a password hash for NMEALOG_PASSWORD_HASH")
    sub.add_parser("init-db", help="create the database if it does not exist")

    hc = sub.add_parser("healthcheck", help="exit 0 when the service is healthy")
    hc.add_argument("target", choices=["collector", "web"])
    hc.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    handlers = {
        "collector": cmd_collector,
        "simulate": cmd_simulate,
        "hash-password": cmd_hash_password,
        "init-db": cmd_init_db,
        "healthcheck": cmd_healthcheck,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
