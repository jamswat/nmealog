# Operations

## Deployment with Docker

```sh
cd nmealog
docker compose build
```

On first load you will be prompted to create an administrator username and password through the web UI. No environment variables are required.

Optionally create `.env` (do not commit it; it is in `.gitignore`) to set the web port or enable secure cookies:

```sh
NMEALOG_SECURE_COOKIES=1        # when served over HTTPS through nginx
NMEALOG_WEB_PORT=8090           # optional; default 8090
```

To pre-configure credentials via environment variables instead of the web setup (for automation), set these in `.env`:

```sh
NMEALOG_PASSWORD_HASH='scrypt:...'
NMEALOG_USER=admin
```

Generate a password hash with:
```sh
docker compose run --rm --no-deps --entrypoint python web -m nmealog hash-password
```
The hash contains `$`, so keep single quotes when you put it in `.env`.

Start:

```sh
docker compose up -d
docker compose ps                  # both services should show "healthy"
docker compose logs -f collector
```

The services:

| Service | What it runs | Network |
|---|---|---|
| `collector` | device connections, parsing, storage, LAN relay, backups | host (needed for LAN access and mDNS) |
| `web` | the web UI, through gunicorn | bridge, published on `127.0.0.1:${NMEALOG_WEB_PORT}` |
| `simulator` | optional fake NMEA source (`--profile sim`) | bridge |

Both services share the `nmealog-data` volume. `docker compose down` keeps it; `docker compose down -v` deletes all data, including backups.

Port 8080 is a common clash. If `docker compose up` reports *port is already allocated*, set `NMEALOG_WEB_PORT` to a free port.

### Without Docker

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export NMEALOG_DATA_DIR=./data
python -m nmealog collector &
gunicorn -b 127.0.0.1:8000 "nmealog.web:create_app()"
```

On first load the web UI will prompt you to create an administrator account. To pre-configure credentials via environment variables instead, add `NMEALOG_PASSWORD_HASH` and `NMEALOG_USER` to the `export` line (see above for how to generate the hash).

## nginx

The web container listens only on localhost. nginx provides HTTPS and the public address, with its own Basic auth (use the same password so the browser asks once):

```nginx
location /nmealog/ {
    auth_basic "nmealog";
    auth_basic_user_file /etc/nginx/htpasswd-nmealog;
    proxy_pass http://127.0.0.1:8090/;      # trailing slash strips /nmealog/
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Prefix /nmealog;
    client_max_body_size 512m;              # backup uploads
    proxy_read_timeout 300s;                # vacuum and integrity checks
}
```

Create the password file with `htpasswd -c /etc/nginx/htpasswd-nmealog admin`.

Security notes:

- The app trusts `X-Forwarded-*` headers, which is only safe while port 8090 is bound to localhost. Do not publish it.
- Do not expose `/health` or `/api/positions` to the internet. The API requires the same login as the pages.
- Map tiles come from `tile.openstreetmap.org`, so browsers need internet access for the background map. The positions themselves stay on the server.

## Backups and restore

Backups are SQLite files made with the online backup API, so they are consistent even while the collector writes. They are stored in `/data/backups` (in the volume) as `nmealog-YYYYMMDD-HHMMSS.sqlite3`.

- **Automatic:** controlled by *Automatic backups*, *Backup interval* and *Backups to keep*.
- **Manual:** Maintenance → Create backup now.
- **Download:** Maintenance → Backups → Download. Keep a copy off the ship's computer.
- **Restore:** type `RESTORE` next to the backup and press Restore. The current state is backed up first (the new backup appears in the list). Restoring a file from a different schema version is refused.
- **Restore from upload:** Maintenance → Restore from file.

To copy the database off the host without the UI:

```sh
docker compose exec collector python -c "import sqlite3; s=sqlite3.connect('/data/nmealog.sqlite3'); d=sqlite3.connect('/data/copy.sqlite3'); s.backup(d); d.close()"
docker cp nmealog-collector-1:/data/copy.sqlite3 .
```

## Retention, the size cap and disk space

- Sentences and positions older than their retention period are deleted by the automatic maintenance job (default every 60 minutes).
- The size cap deletes the oldest sentences, then the oldest positions, when the database file exceeds the limit.
- Deleted pages are returned to the disk with `incremental_vacuum`, which does not lock the database. A full VACUUM is only needed to shrink the file after large deletions in older databases, and it needs free disk space about equal to the database size.
- Check disk space on the Maintenance page. The status page warns below 1 GB free.

## Growth estimate

The Maintenance page projects how fast the database grows:

1. It counts the rows written in the last 24 hours (or since the first row if that is shorter). It needs at least one hour of data.
2. It works out rows per day for sentences and positions.
3. It works out bytes per row from the database itself, using the `dbstat` virtual table, which measures each table and its indexes. If the SQLite build lacks `dbstat`, it falls back to an estimate based on the average sentence length plus fixed overhead, and says so.
4. Growth per day is rows per day times bytes per row. Steady-state size is growth per day times the retention period. If a table has no retention, its steady state is shown as unbounded.
5. Against the size cap it shows the number of days until the cap is reached, or, if the cap is already reached, how many days of data are kept once it is.

The estimate uses the recent rate, so it changes when the ship's activity or the number of devices changes. Logging paused means the sentence rate is zero and the estimate reflects that.

## Health and troubleshooting

| Symptom | Where to look |
|---|---|
| "The collector is not running" | `docker compose logs collector`; the collector writes a heartbeat every 10 s |
| Device says *connect failed* | Terminal server reachable? Port right? Use **Test connection** on the device |
| Device says *sent N bytes but no valid NMEA* | Output format or baud rate on the device, or binary output (see the notes in the README) |
| Clock warning | GPS time differs from the server clock by more than 5 s; check NTP on the server |
| Relay "cannot listen" | Another process uses the port (`ss -ltnp`) |
| Relay advertised but clients do not see it | Multicast blocked on the network (some Wi-Fi APs), or the collector is not on host networking |
| Map is blank | Browser cannot reach tile.openstreetmap.org; positions list still shows in the status line |
| Every login fails | Credentials created through the web UI were forgotten (the database was replaced); or if using env vars, the password hash in `.env` was not single-quoted, so compose replaced the `$` parts |

Health endpoint: `GET /health` returns `{"ok": true}` when the database is readable. The Docker healthchecks run `python -m nmealog healthcheck collector` and `healthcheck web`.
