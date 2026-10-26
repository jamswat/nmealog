# Architecture

## Processes

- **Collector** (`python -m nmealog collector`). One manager thread and one thread per enabled device. Each device thread holds one TCP connection to its terminal server. The manager reloads devices and settings every 2 seconds, starts and stops device threads to match, runs the LAN relay if enabled, writes a heartbeat every 10 seconds, and runs the automatic maintenance and backup schedule.
- **Web** (gunicorn, one worker with threads). Server-rendered HTML, a small JSON endpoint for the map, and manual maintenance actions. Each request opens its own SQLite connection.

The two processes share only the SQLite database file (WAL mode) and the backups directory. The web UI never talks to the collector directly; it changes the database, and the collector picks the change up.

## Data flow

```
terminal server :4001 ──TCP──► device thread ──parse──► batch (1 s or 20 000 rows)
                                    │                      │
                                    │                      ├─► sentences (raw + parsed fields JSON)
                                    │                      ├─► positions (valid fixes, every N s)
                                    │                      ├─► latest (last value per sentence + field)
                                    │                      └─► device_status (counters, GPS time)
                                    └─► LAN relay (optional) ──TCP :10110 / mDNS
```

Each device thread:

- reads bytes with a 1-second timeout, so it can notice shutdown and idle time;
- splits lines on `\n`, drops any line over 512 bytes (NMEA sentences are at most 82 characters);
- validates the checksum; a bad checksum is counted and not used for positions or live values;
- parses with pynmea2 for known talker sentences, and falls back to generic field names (`field_0`, ...) for anything else, including proprietary `$P...` and AIS `!...` sentences;
- takes a position only from a valid fix (GGA quality > 0, RMC or GLL status A), rejects latitude and longitude out of range, and rejects exactly 0,0;
- writes in batches in one transaction every second, to limit fsyncs. If a write fails, the batch is kept (up to 20 000 rows) and retried, and the error appears on the status page.

Reconnection uses exponential backoff from 1 s up to 60 s. A device that sends nothing for `idle_timeout_s` is reconnected. A device that closes the connection is reconnected after 1 s.

## Database

Schema version 1, created on first start. `auto_vacuum=INCREMENTAL` is set on new files, so space can be reclaimed without a full VACUUM.

| Table | Contents | Key indexes |
|---|---|---|
| `meta` | schema version, heartbeat, last maintenance and backup times | primary key |
| `settings` | one row per setting (JSON value) | primary key |
| `devices` | name, host, port, enabled, notes | unique name |
| `device_status` | state, last seen, last error, counts, bytes received, GPS time, clock offset | primary key device |
| `sentences` | received time (UTC epoch), talker, sentence type, checksum result, raw text, parsed fields as JSON | time; device + type + time |
| `positions` | received time, latitude, longitude, source, fix quality, speed, course, satellites, HDOP | time |
| `latest` | last value per device, sentence type and field | primary key (WITHOUT ROWID) |
| `maintenance_log` | who changed what, when | none |

Times are stored as UTC epoch seconds. Display uses the timezone in settings.

## Retention and size cap

Each maintenance run:

1. deletes sentences and positions older than their retention, in chunks of 5000 rows, so no single transaction holds the write lock for long;
2. if the file is over the size cap, repeatedly deletes the oldest 5000 sentences (or, when there are none, positions), then runs `incremental_vacuum` and a WAL checkpoint, and measures the file again.

The cap is checked against the file size on disk, including the WAL.

## Maintenance actions

All actions from the Maintenance page run in the web process, except the scheduled ones, which run in the collector. Restore uses the SQLite backup API to copy the uploaded or chosen file into the live database, so the collector keeps its connections. The file is validated first: integrity check, required tables present, and schema version matching.

## Web security

- HTTP Basic auth on every page. API routes use IP-based access control instead of Basic Auth (see the API docs). The password is stored as a Werkzeug hash in an environment variable. The app refuses to start without it, unless `NMEALOG_ALLOW_NO_AUTH` is set explicitly.
- Every POST requires a CSRF token from the session cookie. The session key is generated once and stored in the data volume with mode 0600.
- Destructive actions need a typed confirmation (`DELETE`, `RESTORE`).
- Backup names are checked against a fixed pattern, so downloads and deletes cannot reach outside the backups directory.
- Uploads are capped at 512 MB.
- Device hosts are validated as IPv4 addresses or host names; ports as 1 to 65535.

## Vendored front-end code

- `static/leaflet.js` and `static/leaflet.css`: Leaflet 1.9.4, copied from unpkg. Update by replacing both files.
- `static/map.js`: the map page script. It builds popups with `textContent`, not HTML, so position data cannot inject markup.

## Tests

`pytest` runs about 98 tests

- `test_nmea.py`: checksums, typed and generic parsing, position rules, the captured live GPS file (`tests/fixtures/live_gps_gbaships.nmea`).
- `test_settings.py`: defaults, validation, storage, logging of changes.
- `test_maintenance.py`: retention, size cap (oldest first), clearing a device, backups and their retention, restore, rejection of bad files, growth estimate.
- `test_collector.py`: a simulator server on localhost; parsing and storage, disabled devices, paused logging, reconnects, unreachable devices, relay forwarding, and a guard against method names that clash with `threading.Thread` internals.
- `test_relay.py`: fan-out to several clients, removal of disconnected clients, port conflicts, advertising.
- `test_web.py`: auth, CSRF, validation, device and settings changes, maintenance actions, backup download and restore, CSV export, map and API, the device test page.
