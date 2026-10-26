# API Reference

nmealog exposes three read-only JSON APIs:

| # | Endpoint | Format | Purpose |
|---|----------|--------|---------|
| 1 | `/signalk/v1/api/` | [Signal K](https://signalk.org/specification/1.8.4/doc/) v1.8.4 | Position, GNSS, heading, depth, wind and weather in the standard marine resource tree |
| 2 | `/api/v1/positions` / `.geojson` | JSON + GeoJSON (RFC 7946) | Position history as points or a FeatureCollection |
| 3 | `/api/v1/current` | Simple JSON | Latest position + live values per device |

All three serve the same data in different shapes for different consumers.

---

## Enabling the API

1. **Settings** → **API** → tick **Enable JSON API** and save.
2. Optionally set **Allowed IP subnets** to restrict access (e.g. `192.168.0.0/24,10.0.0.0/8`).
3. An empty subnet list allows any IP when the API is enabled.

> The API uses basic IP‑based access control, not the web UI's HTTP Basic Auth.
> When the API is **disabled**, all three endpoints return 404.
> When a client IP is **not allowed**, they return 403.

### Nginx passthrough

The real client IP is taken from `request.remote_addr`. When you set
`NMEALOG_TRUST_PROXY=1` (the default in docker-compose), Flask's ProxyFix
extracts the real IP from the `X-Forwarded-For` header that nginx sets.
Subnet checks then run against that real IP.

---

## 1. Signal K API

`/signalk/v1/api/` — returns the API root with version info.

`/signalk/v1/api/vessels/` — returns every device as a Signal K vessel. The
`self` key aliases the vessel with the most recent position fix.

`/signalk/v1/api/vessels/self/` — the full navigation tree for the primary
device.

`/signalk/v1/api/vessels/self/navigation/position` — just the position subtree.

`/signalk/v1/api/vessels/<vessel‑urn>/...` — any specific vessel.

### Populated Signal K paths

| NMEA source | Signal K path | Units |
|-------------|---------------|-------|
| GGA / GNS / RMC / GLL | `navigation.position.value` (latitude, longitude) | deg |
| GGA / GNS | `navigation.gnss.antennaAltitude.value` | m |
| GGA / GNS | `navigation.gnss.geoidalSeparation.value` | m |
| VTG / RMC | `navigation.speedOverGround.value` | m/s |
| VTG / RMC | `navigation.courseOverGroundTrue.value` | rad |
| GGA / GNS | `navigation.gnss.methodQuality.value` | enum (see below) |
| GGA / GNS | `navigation.gnss.satellites.value` | count |
| GGA / GNS / GSA | `navigation.gnss.horizontalDilution.value` | — |
| GSA | `navigation.gnss.positionDilution.value` | — |
| HDG | `navigation.headingCompass.value` (raw sensor reading) | rad |
| HDG + deviation | `navigation.headingMagnetic.value` (derived) | rad |
| HDG + deviation + variation | `navigation.headingTrue.value` (derived) | rad |
| HDT / THS | `navigation.headingTrue.value` | rad |
| HDM | `navigation.headingMagnetic.value` | rad |
| HDG | `navigation.magneticDeviation.value` | rad |
| HDG / RMC | `navigation.magneticVariation.value` | rad |
| ROT | `navigation.rateOfTurn.value` | rad/s |
| DBT / DPT | `environment.depth.belowTransducer.value` | m |
| MDA | `environment.outside.pressure.value` | Pa |
| MDA | `environment.outside.temperature.value` | K |
| MDA | `environment.water.temperature.value` | K |
| MDA | `environment.outside.relativeHumidity.value` | ratio |
| MDA | `environment.outside.dewPointTemperature.value` | K |
| MDA | `environment.wind.directionTrue.value` / `directionMagnetic` | rad |
| MDA | `environment.wind.speedTrue.value` | m/s |
| MWV (`R`) | `environment.wind.angleApparent.value` / `speedApparent` | rad / m/s |
| MWV (`T`) | `environment.wind.angleTrueWater.value` / `speedTrue` | rad / m/s |

Each leaf node includes `$source`, `timestamp` (ISO 8601), `pgn` and
`description`.

Notes on how values are chosen:

- **Units.** All angles are radians and all speeds metres per second, as Signal K requires. Relative wind angles (`angleApparent`, `angleTrueWater`) are signed: negative to port, positive to starboard. HDG variation and deviation are signed too: East is positive and West is negative.
- **Position** comes from the positions table, which only stores fixes that pass validation. Raw NMEA latitude and longitude are never published, because they are in degrees and minutes. While a combined `GN` fix is fresh, single-constellation fixes (`GP`, `GL`) are not stored, so the track does not jump between solutions.
- **Headings.** The HDG heading is the raw compass reading, published as `headingCompass`. Magnetic heading is that reading plus the signed deviation, and true heading is magnetic plus the signed variation, as the NMEA 0183 HDG notes describe. A derived value is only published when its inputs are present.
- **Freshness.** Each value has a validity period, and values older than it are left out of the tree. Every leaf carries `meta.timeout` with that period in seconds, as the Signal K metadata section describes. Most navigation values use 30 s, position and GNSS use 60 s, environment values 300 s, and magnetic variation and deviation 600 s.
- **`methodQuality`** uses the Signal K enum: `no GPS`, `GNSS Fix`, `DGNSS fix`, `Precise GNSS`, `RTK fixed integer`, `RTK float`, `Estimated (DR) mode`, `Manual input`, `Simulator mode`, `Error`.
- **Status flags.** A sentence's own status is respected. Data from an RMC with status `V` or a mode other than `A`/`D`, a VTG with mode `N`, a ROT with status `V`, or a THS with status `V` is not published. A GNS or GGA with no fix still reports `methodQuality` as `no GPS`.
- **Satellites in view** (GSV) are not part of the Signal K tree, because the specification has no path for them. The count, summed across all talkers (`GP`, `GL`, `GA`, `GB`, ...), is available as a live value in `/api/v1/current`.
- **GSA dilution** uses the combined `GN` solution when one is current, and the per-constellation sentence otherwise.

### Example

```sh
curl http://nmealog-host:8090/signalk/v1/api/vessels/self/navigation/position
```

```json
{
  "value": { "latitude": 50.775, "longitude": -1.295 },
  "$source": "nmealog.gps",
  "timestamp": "2024-01-15T12:00:00.000Z",
  "pgn": 129025,
  "description": "Position"
}
```

---

## 2. GeoJSON Position History

`GET /api/v1/positions` — returns a JSON array of position points (same shape
as the internal `/api/positions` used by the map).

`GET /api/v1/positions.geojson` — returns a GeoJSON FeatureCollection.

`GET /api/v1/positions?format=geojson` — same, forces GeoJSON.

### Query parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `device` | int | Filter by device id |
| `since` | float | Unix epoch, inclusive lower bound |
| `until` | float | Unix epoch, inclusive upper bound |
| `limit` | int | Max rows (default 20000, max 20000) |
| `format` | str | `"geojson"` to force GeoJSON output |

### Example (GeoJSON)

```sh
curl 'http://nmealog-host:8090/api/v1/positions.geojson?since=1730000000&device=1'
```

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [-1.295, 50.775]
      },
      "properties": {
        "deviceId": 1,
        "deviceName": "GPS",
        "receivedAt": "2024-10-27T12:00:00.000Z",
        "source": "GGA",
        "fixQuality": 1,
        "speedKn": 8.5,
        "courseTrue": 90.0,
        "satellites": 8,
        "hdop": 0.9
      }
    }
  ]
}
```

Coordinates are **[longitude, latitude]** per RFC 7946.

### Consuming with Python

```python
import geojson
import requests

r = requests.get("http://nmealog-host:8090/api/v1/positions.geojson")
fc = geojson.loads(r.text)
assert fc["type"] == "FeatureCollection"
for feature in fc["features"]:
    lon, lat = feature["geometry"]["coordinates"]
    print(lat, lon, feature["properties"]["speedKn"])
```

---

## 3. Current State

`GET /api/v1/current` — returns a snapshot of every device with its current
position and live NMEA values.  No time‑range filtering — always the latest.

### Example

```sh
curl http://nmealog-host:8090/api/v1/current
```

```json
{
  "server": {
    "version": "1.3.0",
    "time": "2024-10-27T12:00:00.000Z",
    "collectorAlive": true
  },
  "devices": [
    {
      "id": 1,
      "name": "GPS",
      "host": "192.168.1.100",
      "port": 4001,
      "enabled": true,
      "state": "connected",
      "lastSeen": "2024-10-27T12:00:00.000Z",
      "lastError": null,
      "sentencesOk": 15234,
      "sentencesBad": 12,
      "bytesIn": 1928374,
      "position": {
        "latitude": 50.775,
        "longitude": -1.295,
        "source": "GGA",
        "fixQuality": 1,
        "speedKn": 8.5,
        "courseTrue": 90.0,
        "satellites": 8,
        "hdop": 0.9,
        "receivedAt": "2024-10-27T12:00:00.000Z"
      },
      "values": {
        "GPGGA": {
          "gps_qual": { "value": "1", "receivedAt": "..." },
          "latitude": { "value": "5030.1234", "receivedAt": "..." }
        }
      }
    }
  ]
}
```

---

## Client libraries

### GeoJSON

- **Python**: [`geojson`](https://pypi.org/project/geojson/) — validate and manipulate FeatureCollections.
- **JavaScript**: Leaflet, Mapbox, turf.js — all natively consume GeoJSON.

### Signal K

- **Python**: Use the `requests` library to fetch JSON and navigate the resource
  tree.  No full Signal K client library is required for these read-only
  endpoints.
- **JavaScript / Node**: The [`signalk-client`](https://npmjs.com/package/signalk-client)
  package can connect to any Signal K server.

---

## Security notes

- The API is **off by default**.  You must explicitly enable it in Settings.
- Access control is **IP‑based via allowed CIDR subnets**.  No password or token
  is required — the trust model is "the network is the firewall."
- When behind nginx, set `NMEALOG_TRUST_PROXY=1` (the default in
  docker-compose) so the real client IP is extracted from `X-Forwarded-For`.
- The web UI's HTTP Basic Auth and CSRF protection **do not** apply to the API
  endpoints.