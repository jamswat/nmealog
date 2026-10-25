# nmealog

Records NMEA 0183 data from serial-to-TCP server into SQLite, shows it in a web interface, and can re-serve a chosen GPS to the local network. (supports Signal K, GeoJSON, and a basic live location JSON api)

## Features

- **Collection:** one persistent connection per device, with reconnect and backoff. Checksums are checked. Known sentences are parsed in full (GGA, RMC, GLL, VTG, GSA, GSV, ZDA, HDT, HDG, ROT, DBT, MWV, DTM and more); unknown and proprietary sentences are kept with generic field names.
- **Storage:** every sentence with its parsed fields, positions from valid fixes at a set interval, and the latest value of every field per device.
- **Retention and size cap:** delete data older than N days, and keep the database under N MB by removing the oldest data first.
- **Web UI:** status, track table, [map](docs/operations.md) (OpenStreetMap), sentence browser, device management with a connection test, settings, maintenance.
- **Maintenance:** prune, size cap, reclaim, checkpoint, optimize, integrity check, vacuum, delete by date, scheduled and manual backups, download, restore, activity log, and a [growth estimate](docs/operations.md#growth-estimate) with days until the size cap.
- **LAN relay:** optional. Re-serves one device's sentences on TCP port 10110 and advertises them over mDNS as `_nmea-0183._tcp`, so GeoClue, and some and similar clients pick up the position. **Off by default**, because it publishes the position to the whole network. See [docs/lan-relay.md](docs/lan-relay.md).
- **Settings in the database:** every option is editable in the UI and takes effect without a restart. See [docs/settings.md](docs/settings.md).

## Documentation

| Document | For |
|---|---|
| [docs/operations.md](docs/operations.md) | deploying with Docker, nginx, backups and restore, retention and disk space, troubleshooting |
| [docs/settings.md](docs/settings.md) | every setting, its default and its limits |
| [docs/lan-relay.md](docs/lan-relay.md) | the `_nmea-0183._tcp` relay, its security, and how to test it |
| [docs/api.md](docs/api.md) | Signal K, GeoJSON and Current-state JSON APIs |
| [docs/architecture.md](docs/architecture.md) | processes, data flow, schema, retention algorithm, security, tests |

## Quick start (Docker)

```sh
cd nmealog
docker compose build
docker compose up -d
```

Open `http://127.0.0.1:8090` (or put nginx in front; see [operations](docs/operations.md#nginx)).

On first load you will be prompted to create an administrator username and password. Once set, you will need to sign in with those credentials on every visit.

In **Devices**, add the GPS, gyro, etc. Use **Test connection** on each one first to see the raw lines it sends.

To try the system without a ship ;) - run `docker compose --profile sim up -d` and add a device with host `simulator`, port `4001`.

## Local development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Layout:

```
nmealog/
  nmealog/        application code
    collector.py  device connections, parsing, batched writes, manager
    relay.py      LAN relay and mDNS advertising
    nmea.py       checksums, pynmea2 parsing, generic fallback
    maintenance.py retention, size cap, backups, restore, growth estimate
    settings.py   setting definitions, validation, storage
    web.py        Flask app: auth, CSRF, pages, JSON API
    simulator.py  NMEA TCP simulator for testing
    templates/ static/
  tests/          pytest suite and a captured live GPS sample
  docs/           documentation
  Dockerfile, docker-compose.yml, requirements*.txt
```

