# Settings reference

All settings are stored in the database (`settings` table) and changed in **Settings** in the web UI. The collector reloads them every few seconds, so changes apply without a restart. Values that are not set keep their default. Changes are recorded in the activity log.

Only the web login and data location are environment variables; see [operations.md](operations.md).

| Setting | Key | Default | Allowed | Group | What it does |
|---|---|---|---|---|---|
| Logging enabled | `logging_enabled` | on | on / off | Logging | When off, devices stay connected and live values update, but no sentence or position history is written. |
| Store every sentence | `store_sentences` | on | on / off | Logging | Keep each received sentence with its parsed fields. Turn off to keep only positions and live values. |
| Store sentences with bad checksums | `store_invalid` | off | on / off | Logging | Bad checksums usually mean line noise or a wiring fault. |
| Position sample interval | `position_interval_s` | 60 | 1 to 86400 s | Logging | Seconds between stored positions for each device. |
| Keep sentences for | `sentence_retention_days` | 14 | 0 to 36500 days | Storage | Older sentences are deleted. 0 = keep forever. |
| Keep positions for | `position_retention_days` | 180 | 0 to 36500 days | Storage | Older positions are deleted. 0 = keep forever. |
| Maximum database size | `max_db_size_mb` | 100 | 0 to 100000 MB | Storage | When exceeded, the oldest sentences are deleted first, then the oldest positions. 0 = no limit. |
| Automatic pruning and size limit | `auto_maintenance` | on | on / off | Maintenance | Apply the retention and size settings on a schedule. |
| Maintenance check interval | `maintenance_interval_min` | 60 | 1 to 10080 min | Maintenance | - |
| Automatic backups | `auto_backup` | on | on / off | Maintenance | Take a consistent database backup on a schedule. |
| Backup interval | `backup_interval_h` | 24 | 1 to 8760 h | Maintenance | - |
| Backups to keep | `backup_keep` | 14 | 1 to 1000 | Maintenance | Older backups beyond this count are deleted. |
| Connect timeout | `connect_timeout_s` | 5 | 1 to 60 s | Connection | - |
| Reconnect after silence of | `idle_timeout_s` | 60 | 5 to 3600 s | Connection | Reconnect if a device sends nothing for this long. |
| Show device as silent after | `stale_after_s` | 30 | 1 to 3600 s | Connection | - |
| Display timezone | `display_tz` | UTC | text | Display | IANA name, for example Europe/London. Data is always stored in UTC. |
| Advertise positions on the LAN | `relay_enabled` | off | on / off | LAN relay | Serves the chosen device's sentences on a TCP port and advertises them over mDNS as _nmea-0183._tcp. Anyone on the local network can read them, including position. Off by default. |
| Source device | `relay_device_id` | None | a device, or None | LAN relay | The device whose valid sentences are relayed. Choose none to disable. |
| TCP port | `relay_port` | 10110 | 1 to 65535 | LAN relay | The port clients connect to. 10110 is the conventional NMEA port. |
| Advertise over mDNS | `relay_advertise` | on | on / off | LAN relay | Lets Linux (GeoClue, Gnome Maps), Mac and other clients find the relay without typing an address. |
| Advertised name | `relay_name` | nmealog | text | LAN relay | Shown to clients browsing for NMEA services. |

Notes:

- **Retention** and **size cap** are applied by the automatic maintenance job. The manual buttons on the Maintenance page apply the same rules immediately.
- **Size cap** deletes the oldest sentences first, then the oldest positions, until the database file is under the limit. Free pages are then returned to the disk.
- **LAN relay** settings take effect when the collector notices the change (within a few seconds). Changing the port or advertise option restarts the relay. Changing only the source device does not.
