# LAN relay (`_nmea-0183._tcp`)

The relay re-serves the NMEA stream from one chosen device on a TCP port, and advertises it on the local network over mDNS (Bonjour / Avahi) as `_nmea-0183._tcp`. This is the same mechanism used by the static GPS server in Evert Pot's article, [Broadcasting GPS on the local network](https://evertpot.com/broadcasting-gps-on-local-network/). Clients that understand the service, such as GeoClue on Linux (used by Gnome Maps and Firefox), read the position from it without being told an address.

## Security: read this first

Anyone on the same network segment can connect to the relay and read the stream, including the ship's position and speed. The relay is **off by default**. Turn it on only on a network you trust, or leave it off.

## How it works

- The collector passes every valid sentence from the source device to the relay (the source is chosen in Settings → LAN relay). Bad-checksum sentences are never relayed.
- The relay runs whether or not logging is paused.
- Each connected client gets every sentence, one line per sentence, CRLF-terminated, with no client commands. Clients that stop reading are disconnected.
- Up to 2000 sentences are buffered between the collector and the relay. If the relay falls behind, the excess is dropped and counted (shown on the status page as "dropped").
- The advertisement lists the host's IPv4 addresses, excluding loopback and Docker or virtual bridges, which other machines cannot reach.
- If the port is already in use, or mDNS cannot start, the error is shown on the status page and in the collector log. The TCP relay keeps running when only mDNS fails.

## Setup

1. Put the collector on host networking (already the case in `docker-compose.yml`). Containers on the default bridge cannot advertise on the LAN.
2. Settings → LAN relay: tick **Advertise positions on the LAN**, choose the source device (for example the GPS), leave the port at 10110, and save.
3. Check the status page's **LAN relay** section: it should say *serving on port 10110* and *advertised*.

Test from any machine on the LAN:

```sh
telnet <host-ip> 10110            # raw sentences
avahi-browse _nmea-0183._tcp -r -t    # Linux; shows the advertised record
dns-sd -B _nmea-0183._tcp             # macOS
```

For GeoClue on Linux, enable network NMEA sources in `/etc/geoclue/geoclue.conf`:

```ini
[network-nmea]
enable=true
```

then `sudo systemctl restart geoclue` and run `/usr/libexec/geoclue-2.0/demos/where-am-i`.
