# Weather Station Bridge

A small Home Assistant add-on that catches a personal weather station's Weather Underground upload on your own network, turns it into local MQTT sensors in Home Assistant, and still forwards a copy to Weather Underground.

> **Disclaimer.** This is a personal project, shared as-is. I have run it on my own Home Assistant with a Sainlogic WS0835 Plus since November 2026 with little to no issue, but it is not a polished or supported product. It is published mainly so that anyone with a similar plan, getting a cloud-only weather station into Home Assistant locally, can use it as inspiration or a starting point. Read the code, test it on your own setup, and adapt it. No warranty (see `LICENSE`). Not affiliated with Weather Underground, The Weather Company or Sainlogic.

## Why

A Sainlogic WS0835 Plus (and other stations that upload with the Wunderground protocol) sends one plain-HTTP GET a minute to `rtupdate.wunderground.com`. The Sainlogic app and the console's own web page have no custom-server option, so this add-on takes the upload over with a DNS rewrite instead:

```
station ──GET──> rtupdate.wunderground.com ──(DNS rewrite)──> this add-on :80
                                                                │
                                          ┌─────────────────────┴──────────────┐
                                          ▼                                    ▼
                          MQTT (Mosquitto) + HA discovery        forward untouched request to the
                          sensors appear in Home Assistant       real Weather Underground (public IP)
```

Weather Underground keeps receiving data, so anything that depends on it keeps working. The station's separate Weathercloud upload is not touched.

## Files

| File | Purpose |
|---|---|
| `weather_station_bridge/config.yaml` | Add-on manifest and options |
| `weather_station_bridge/Dockerfile` | `python:3.12-alpine` plus pinned `paho-mqtt` and `dnspython` |
| `weather_station_bridge/receiver.py` | The receiver: HTTP listener, MQTT publisher, forwarder |

## Install

1. In Home Assistant, create a dedicated user for the add-on (**Settings → People → Users**, for example `weather`). The Mosquitto add-on authenticates against HA users.
2. Copy the `weather_station_bridge/` folder from this repo to HA's `/addons` share as `/addons/weather_station_bridge/` (Samba share `addons`, the SSH add-on, or Studio Code Server).
3. **Settings → Apps → App Store → ⋮ → Check for updates** (called Add-ons before Home Assistant 2026.2), then open **Local apps → Weather Station Bridge → Install**. If the Local apps section does not appear, refresh the browser page; an invalid `config.yaml` is skipped silently.
4. On the **Configuration** tab set `station_id`, `mqtt_username` and `mqtt_password`. Leave the rest at their defaults unless the table below says otherwise.
5. Start it and check the **Log** tab. It should print `listening on :80`, `MQTT connected`, and nothing else until the station reports.
6. Make the station reach it (next section).

## Make the station reach it

The station must request `rtupdate.wunderground.com` and get the add-on's address back.

- **DNS rewrite:** add `rtupdate.wunderground.com` → the HA host's IP in AdGuard Home (**Filters → DNS rewrites**). With an origin/replica pair, add it on the origin and let the sync copy it.
- **Firewall:** if the station is on a different VLAN than Home Assistant, allow the station's IP to reach the HA host on TCP 80.
- **Check:** within a minute the add-on log shows `Update from <station>: N readings` and `Forwarded to rtupdate.wunderground.com: HTTP 200 success`. The new sensors appear under the device named by `device_name`.

To undo it, delete the rewrite. The station goes straight back to Weather Underground.

## Options

| Option | Default | Notes |
|---|---|---|
| `station_id` | empty (**required**) | Your Weather Underground station ID. Updates for any other ID are refused and not forwarded; the app will not start while it is empty |
| `device_name` | `Local Weather Station` | Entity IDs come from this, for example `sensor.local_weather_station_temperature` |
| `allowed_source_ip` | empty | Set to the station's IP to refuse every other source. Use a DHCP reservation first |
| `mqtt_host` / `mqtt_port` | `core-mosquitto` / `1883` | The Mosquitto add-on |
| `mqtt_username` / `mqtt_password` | empty | Required if the broker needs a login |
| `topic_prefix` | `weather_station_bridge` | State goes to `<prefix>/<station>/state`, availability to `<prefix>/<station>/status` |
| `forward_to_wunderground` | `true` | Turn off to keep the data local only |
| `wunderground_host` | `rtupdate.wunderground.com` | Real-time endpoint the station uses |
| `upstream_dns` | `1.1.1.1` | Used only to look up the real Wunderground address, so the rewrite cannot catch the add-on's own forward. A private answer is refused to prevent a loop |
| `log_level` | `info` | `debug` also logs the parsed readings (never the station key) |

## What it does with the request

- Answers the station `200 success` straight away, then publishes and forwards in the background. The station never learns whether the forward worked.
- Publishes one JSON state message per update (not retained) and discovery configs for each sensor the station actually sends (retained). Sensors go unavailable after 5 minutes without an update.
- Adds a `Last update` diagnostic timestamp from the station's `dateutc`.
- Never logs or publishes the station's `PASSWORD` key. It is only passed through on the forwarded request.
- Re-announces its sensors after an MQTT reconnect and when Home Assistant restarts.

Sensors it knows about: temperature, humidity, dew point, wind chill, heat index, indoor temperature and humidity, relative and absolute pressure, wind speed, gust, direction, 2-minute average wind, 10-minute gust, rain (last hour, hourly, daily, weekly, monthly, yearly), solar radiation, UV. Any other field in the request is ignored.

The daily, weekly, monthly and yearly rain sensors are labelled "to date" because the Wunderground protocol defines them as rain so far today, this week, this month and this year (calendar periods, not rolling windows). That is the protocol's definition, not something confirmed for the Sainlogic firmware: to check, watch each sensor's history graph around midnight on the 1st (month) and on the first day of the week. A drop to zero at that moment means a calendar reset; a slow decline means a rolling window. Renaming does not change existing entity IDs (they stay `..._rain_this_month` and so on).

**Calculated sensors.** The station does not send these, so the app works them out from the readings it does get:

| Sensor | How it is calculated |
|---|---|
| `Heat index` | NWS Rothfusz regression with the NWS low- and high-humidity adjustments. Only defined from about 80 °F; below that it equals the air temperature, as on weather.gov. If a station ever sends its own `heatindexf`, that value is used instead. |
| `Feels like` | NWS-style apparent temperature: wind chill when it is 50 °F or colder and the wind is over 3 mph, heat index at 80 °F or warmer, otherwise the air temperature. Wind chill uses the NWS formula (it matched the station's own wind chill within about 0.1 °F in two checks). |
| `Absolute humidity` | Water vapour in g/m³, from temperature and relative humidity. Useful for comparing the moisture content of indoor and outdoor air, which relative humidity cannot do. |

These were checked against the NWS heat index chart (90 °F/70% gives 106, 100 °F/50% gives 118, 96 °F/65% gives 121). They are only as accurate as the station's temperature and humidity, and a reading with humidity outside 0–100% is skipped. Not calculated here on purpose: pressure trend (a 3-hour change is better done in Home Assistant with a derivative or statistics helper, which keeps its history across restarts), and dew point and wind chill, which the station already sends.

Wind direction also gets text sensors that show a 16-point compass abbreviation (N, NNE, NE, ENE, E, and so on) next to the numeric degrees: `Wind direction (compass)`, `Wind direction (2 min avg, compass)` and `Wind gust direction (10 min, compass)`. The degrees assume the station's arrow points north; if it is mounted off, the compass reading is off by the same amount.

## Known limits

- The station's units stay imperial in MQTT; Home Assistant converts them for display.
- If the station is later moved to a firmware that uses HTTPS, this stops working and the add-on log goes quiet.
- If Weather Underground changes its endpoint host, update `wunderground_host`.
- Tested on Home Assistant 2026.8 (where add-ons are called Apps) with a Sainlogic WS0835 Plus: updates arrive every minute, sensors appear under the device, and Weather Underground still receives the forwarded copy.
- The station's `winddir_avg2m` has read 0° while the instant wind direction read 315°, so treat the 2-minute average direction as unreliable; the bridge passes on whatever the station sends.

## Credits

Written with help from Claude (Anthropic's AI assistant); designed, tested and run by me on my own setup.
