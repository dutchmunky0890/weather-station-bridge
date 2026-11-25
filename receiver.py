#!/usr/bin/env python3
"""Weather Station Bridge.

Receives the Wunderground-protocol GET a personal weather station sends
(/weatherstation/updateweatherstation.php?...), publishes the readings to MQTT
with Home Assistant discovery, and forwards the untouched request to the real
Weather Underground endpoint.

The station's upload key (the PASSWORD parameter) is never logged and never
published to MQTT; it is only passed through on the forwarded request.
"""
import http.client
import ipaddress
import json
import logging
import math
import os
import queue
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "0.1.0"
UPDATE_PATH = "/weatherstation/updateweatherstation.php"
EXPIRE_AFTER = 300  # seconds without an update before HA marks sensors unavailable

log = logging.getLogger("bridge")

# key -> (name, unit, device_class, state_class, icon)
SENSORS = {
    "tempf": ("Temperature", "°F", "temperature", "measurement", None),
    "humidity": ("Humidity", "%", "humidity", "measurement", None),
    "dewptf": ("Dew point", "°F", "temperature", "measurement", None),
    "windchillf": ("Wind chill", "°F", "temperature", "measurement", None),
    "heatindexf": ("Heat index", "°F", "temperature", "measurement", None),
    "feelslikef": ("Feels like", "°F", "temperature", "measurement", None),
    "abshumidity": ("Absolute humidity", "g/m³", None, "measurement", "mdi:water-percent"),
    "indoortempf": ("Indoor temperature", "°F", "temperature", "measurement", None),
    "indoorhumidity": ("Indoor humidity", "%", "humidity", "measurement", None),
    "baromin": ("Pressure (relative)", "inHg", "pressure", "measurement", None),
    "absbaromin": ("Pressure (absolute)", "inHg", "pressure", "measurement", None),
    "windspeedmph": ("Wind speed", "mph", "wind_speed", "measurement", None),
    "windgustmph": ("Wind gust", "mph", "wind_speed", "measurement", None),
    "winddir": ("Wind direction", "°", None, "measurement", "mdi:compass-outline"),
    "windspdmph_avg2m": ("Wind speed (2 min avg)", "mph", "wind_speed", "measurement", None),
    "winddir_avg2m": ("Wind direction (2 min avg)", "°", None, "measurement", "mdi:compass-outline"),
    "windgustmph_10m": ("Wind gust (10 min max)", "mph", "wind_speed", "measurement", None),
    "windgustdir_10m": ("Wind gust direction (10 min)", "°", None, "measurement", "mdi:compass-outline"),
    "rainin": ("Rain (last hour)", "in", None, "measurement", "mdi:weather-rainy"),
    "hourlyrainin": ("Rain (hourly)", "in", "precipitation", "total_increasing", None),
    "dailyrainin": ("Rain (today)", "in", "precipitation", "total_increasing", None),
    "weeklyrainin": ("Rain (week to date)", "in", "precipitation", "total_increasing", None),
    "monthlyrainin": ("Rain (month to date)", "in", "precipitation", "total_increasing", None),
    "yearlyrainin": ("Rain (year to date)", "in", "precipitation", "total_increasing", None),
    "solarradiation": ("Solar radiation", "W/m²", "irradiance", "measurement", None),
    "UV": ("UV index", "UV index", None, "measurement", "mdi:sun-wireless-outline"),
}


# Display precision hints for HA (default 1). Rain and pressure need two decimals,
# or HA rounds 0.05 in to "0 in".
DISPLAY_PRECISION = {
    "humidity": 0,
    "indoorhumidity": 0,
    "winddir": 0,
    "winddir_avg2m": 0,
    "windgustdir_10m": 0,
    "baromin": 2,
    "absbaromin": 2,
    "rainin": 2,
    "hourlyrainin": 2,
    "dailyrainin": 2,
    "weeklyrainin": 2,
    "monthlyrainin": 2,
    "yearlyrainin": 2,
}


COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]
# source key -> (derived key, entity name): text sensors that show the direction as a compass point
COMPASS_SENSORS = {
    "winddir": ("winddir_compass", "Wind direction (compass)"),
    "winddir_avg2m": ("winddir_avg2m_compass", "Wind direction (2 min avg, compass)"),
    "windgustdir_10m": ("windgustdir_10m_compass", "Wind gust direction (10 min, compass)"),
}
COMPASS_NAMES = {derived: name for derived, name in COMPASS_SENSORS.values()}


def compass(degrees):
    """Degrees -> 16-point compass abbreviation (0 = N, 90 = E, 315 = NW)."""
    return COMPASS_POINTS[int(degrees % 360 / 22.5 + 0.5) % 16]


def heat_index_f(temp_f, humidity):
    """NWS heat index (Rothfusz regression with the NWS adjustments), in °F.

    The heat index is only defined from about 80 °F up; below that the air
    temperature is returned, which is also what weather.gov shows.
    """
    if temp_f < 80:
        return temp_f
    simple = 0.5 * (temp_f + 61.0 + (temp_f - 68.0) * 1.2 + humidity * 0.094)
    if (simple + temp_f) / 2 < 80:
        return simple
    hi = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * humidity
        - 0.22475541 * temp_f * humidity
        - 0.00683783 * temp_f ** 2
        - 0.05481717 * humidity ** 2
        + 0.00122874 * temp_f ** 2 * humidity
        + 0.00085282 * temp_f * humidity ** 2
        - 0.00000199 * temp_f ** 2 * humidity ** 2
    )
    if humidity < 13 and 80 <= temp_f <= 112:
        hi -= ((13 - humidity) / 4) * math.sqrt((17 - abs(temp_f - 95)) / 17)
    elif humidity > 85 and 80 <= temp_f <= 87:
        hi += ((humidity - 85) / 10) * ((87 - temp_f) / 5)
    return hi


def wind_chill_f(temp_f, wind_mph):
    """NWS wind chill in °F; valid for temp <= 50 °F and wind > 3 mph."""
    factor = wind_mph ** 0.16
    return 35.74 + 0.6215 * temp_f - 35.75 * factor + 0.4275 * temp_f * factor


def feels_like_f(temp_f, humidity, wind_mph):
    """NWS-style apparent temperature: wind chill when cold and windy, heat index when hot."""
    if temp_f <= 50 and wind_mph is not None and wind_mph > 3:
        return wind_chill_f(temp_f, wind_mph)
    if temp_f >= 80 and humidity is not None:
        return heat_index_f(temp_f, humidity)
    return temp_f


def absolute_humidity_gm3(temp_f, humidity):
    """Water vapour density in g/m³ from temperature and relative humidity."""
    temp_c = (temp_f - 32) * 5 / 9
    return (
        6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5)) * humidity * 2.1674
        / (273.15 + temp_c)
    )


def parse_number(text):
    """Return a float, or None for junk and the -9999 'no data' sentinels."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value != value or abs(value) >= 9999:
        return None
    return value


def parse_dateutc(text):
    """'2026-11-11 18:48:18' (UTC) -> ISO string; 'now'/junk -> current UTC time."""
    if text and text != "now":
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            return parsed.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def build_state(query):
    """Pick the known numeric readings out of a parsed query; add last_update."""
    state = {}
    for key in SENSORS:
        if key in query:
            value = parse_number(query[key][0])
            if value is not None:
                state[key] = value
    temp = state.get("tempf")
    humidity = state.get("humidity")
    if humidity is not None and not 0 <= humidity <= 100:
        humidity = None
    if temp is not None:
        wind = state.get("windspeedmph")
        # Only fill in values the station did not send itself.
        if "heatindexf" not in state and humidity is not None:
            state["heatindexf"] = round(heat_index_f(temp, humidity), 1)
        state["feelslikef"] = round(feels_like_f(temp, humidity, wind), 1)
        if humidity is not None:
            state["abshumidity"] = round(absolute_humidity_gm3(temp, humidity), 2)
    for source, (derived, _name) in COMPASS_SENSORS.items():
        if source in state:
            state[derived] = compass(state[source])
    state["last_update"] = parse_dateutc(query.get("dateutc", [""])[0])
    return state


class MqttPublisher:
    """Thin paho wrapper: availability, retained discovery, JSON state."""

    def __init__(self, opts):
        import paho.mqtt.client as mqtt  # imported here so tests can run without paho

        self.station = opts["station_id"].lower()
        self.device_name = opts["device_name"]
        prefix = opts["topic_prefix"].strip("/")
        self.state_topic = f"{prefix}/{self.station}/state"
        self.status_topic = f"{prefix}/{self.station}/status"
        self.announced = set()
        self.lock = threading.Lock()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"weather-station-bridge-{self.station}",
        )
        if opts["mqtt_username"]:
            self.client.username_pw_set(opts["mqtt_username"], opts["mqtt_password"])
        self.client.will_set(self.status_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.connect_async(opts["mqtt_host"], int(opts["mqtt_port"]), keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            log.error("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected")
        with self.lock:
            self.announced.clear()  # re-send discovery with the next update
        client.subscribe("homeassistant/status")
        client.publish(self.status_topic, "online", qos=1, retain=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, message):
        if message.topic == "homeassistant/status" and message.payload == b"online":
            log.info("Home Assistant restarted; will re-announce sensors")
            with self.lock:
                self.announced.clear()

    def _discovery(self, key):
        if key in COMPASS_NAMES:
            name, unit, device_class, state_class, icon = (
                COMPASS_NAMES[key], None, None, None, "mdi:compass-outline",
            )
        else:
            name, unit, device_class, state_class, icon = SENSORS[key]
        config = {
            "name": name,
            "unique_id": f"{self.station}_{key.lower()}",
            "state_topic": self.state_topic,
            "value_template": "{{ value_json['%s'] }}" % key,
            "unit_of_measurement": unit,
            "state_class": state_class,
            "suggested_display_precision": DISPLAY_PRECISION.get(key, 1),
            "expire_after": EXPIRE_AFTER,
            "availability_topic": self.status_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [f"weather_station_bridge_{self.station}"],
                "name": self.device_name,
                "manufacturer": "Sainlogic",
                "model": "WS0835 Plus",
            },
            "origin": {"name": "Weather Station Bridge", "sw_version": VERSION},
        }
        if unit is None:  # compass text sensors have no unit, state class or precision
            for field in ("unit_of_measurement", "state_class", "suggested_display_precision"):
                config.pop(field)
        if device_class:
            config["device_class"] = device_class
        if icon:
            config["icon"] = icon
        return f"homeassistant/sensor/{self.station}/{key.lower()}/config", config

    def _last_update_discovery(self):
        config = {
            "name": "Last update",
            "unique_id": f"{self.station}_last_update",
            "state_topic": self.state_topic,
            "value_template": "{{ value_json['last_update'] }}",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            "availability_topic": self.status_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {"identifiers": [f"weather_station_bridge_{self.station}"]},
            "origin": {"name": "Weather Station Bridge", "sw_version": VERSION},
        }
        return f"homeassistant/sensor/{self.station}/last_update/config", config

    def publish(self, state):
        with self.lock:
            fresh = [k for k in state if k != "last_update" and k not in self.announced]
            need_last_update = "last_update" not in self.announced
            self.announced.update(fresh)
            self.announced.add("last_update")
        for key in fresh:
            topic, config = self._discovery(key)
            self.client.publish(topic, json.dumps(config), qos=1, retain=True)
        if need_last_update:
            topic, config = self._last_update_discovery()
            self.client.publish(topic, json.dumps(config), qos=1, retain=True)
        self.client.publish(self.state_topic, json.dumps(state), qos=0, retain=False)

    def close(self):
        try:
            self.client.publish(self.status_topic, "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:  # shutting down; nothing useful to do
            pass
        self.client.loop_stop()
        self.client.disconnect()


class Forwarder:
    """Forwards the original request to Weather Underground from a worker thread."""

    def __init__(self, opts):
        self.host = opts["wunderground_host"]
        self.upstream_dns = (opts.get("upstream_dns") or "").strip()
        self.queue = queue.Queue(maxsize=50)
        self.cache = None  # (ip, expiry)
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, raw_path):
        try:
            self.queue.put_nowait(raw_path)
        except queue.Full:
            log.warning("Forward queue full; dropping an update")

    def _resolve(self):
        now = time.time()
        if self.cache and self.cache[1] > now:
            return self.cache[0]
        if self.upstream_dns:
            import dns.resolver  # imported here so tests can run without dnspython

            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [self.upstream_dns]
            answer = resolver.resolve(self.host, "A", lifetime=5)
            ip = answer[0].to_text()
            ttl = max(30, min(answer.rrset.ttl, 600))
        else:
            ip = socket.getaddrinfo(self.host, 80, socket.AF_INET)[0][4][0]
            ttl = 60
        if ipaddress.ip_address(ip).is_private:
            # A private answer means a local rewrite caught us; forwarding would loop.
            raise RuntimeError(f"{self.host} resolved to private address {ip}; set upstream_dns")
        self.cache = (ip, now + ttl)
        return ip

    def _worker(self):
        while True:
            raw_path = self.queue.get()
            try:
                ip = self._resolve()
                conn = http.client.HTTPConnection(ip, 80, timeout=10)
                conn.request(
                    "GET", raw_path,
                    headers={"Host": self.host, "User-Agent": f"weather-station-bridge/{VERSION}"},
                )
                response = conn.getresponse()
                body = response.read(64).decode("ascii", "replace").strip()
                conn.close()
                log.info("Forwarded to %s: HTTP %s %s", self.host, response.status, body[:20])
            except Exception as exc:
                self.cache = None
                log.warning("Forward to %s failed: %s", self.host, exc)


class Bridge:
    def __init__(self, opts, publisher, forwarder):
        self.station = opts["station_id"].lower()
        self.allowed_source = (opts.get("allowed_source_ip") or "").strip()
        self.publisher = publisher
        self.forwarder = forwarder
        self.warned_ids = set()

    def process(self, raw_path, client_ip):
        """Return (status, body) for one GET request."""
        parts = urlsplit(raw_path)
        if parts.path != UPDATE_PATH:
            return 404, "not found\n"
        if self.allowed_source and client_ip != self.allowed_source:
            log.warning("Rejected update from unexpected source %s", client_ip)
            return 403, "forbidden\n"
        query = parse_qs(parts.query, keep_blank_values=True)
        station_id = query.get("ID", [""])[0]
        if station_id.lower() != self.station:
            if station_id not in self.warned_ids:
                self.warned_ids.add(station_id)
                log.warning("Ignoring update for station ID %r (expected %r)", station_id, self.station)
            return 403, "forbidden\n"

        state = build_state(query)
        log.info("Update from %s: %d readings", station_id, len(state) - 1)
        log.debug("Readings: %s", state)
        try:
            self.publisher.publish(state)
        except Exception as exc:
            log.warning("MQTT publish failed: %s", exc)
        if self.forwarder:
            self.forwarder.submit(raw_path)
        return 200, "success\n"


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "weather-station-bridge"

        def do_GET(self):
            status, body = bridge.process(self.path, self.client_address[0])
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            pass  # request lines carry the station key; the bridge logs what matters

    return Handler


def load_options():
    path = os.environ.get("OPTIONS_FILE", "/data/options.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main():
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not str(opts.get("station_id", "")).strip():
        log.error("station_id is empty: set it to your Weather Underground station ID on the Configuration tab")
        return 1
    publisher = MqttPublisher(opts)
    forwarder = Forwarder(opts) if opts.get("forward_to_wunderground") else None
    bridge = Bridge(opts, publisher, forwarder)

    port = int(os.environ.get("LISTEN_PORT", "80"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(bridge))
    signal.signal(
        signal.SIGTERM,
        lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
    )
    log.info(
        "Weather Station Bridge %s listening on :%d for station %s (forwarding %s)",
        VERSION, port, opts["station_id"], "on" if forwarder else "off",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        publisher.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
