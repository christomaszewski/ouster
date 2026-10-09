"""ROS-independent observations and diagnostics. See docs/SERVICE_HEALTH.md."""
import math
import json
import threading
import time
import uuid

OK, WARN, ERROR, STALE = 0, 1, 2, 3
METRICS = {"temp.internal_c": ("internal_temperature_deg_c", 1),
           "supply.voltage_v": ("input_voltage_mv", 0.001),
           "supply.current_a": ("input_current_ma", 0.001)}


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def config(raw=None):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("health must be a mapping")
    unknown = set(raw) - {"interval_s", "poll_interval_s", "stale_after_s", "limits"}
    if unknown:
        raise ValueError(f"unknown health settings: {sorted(unknown)}")
    result = {"interval_s": 1.0, "poll_interval_s": 5.0, "stale_after_s": 15.0, "limits": {}}
    result.update(raw)
    for key in ("interval_s", "poll_interval_s", "stale_after_s"):
        if not numeric(result[key]) or result[key] <= 0:
            raise ValueError(f"health.{key} must be a positive finite number")
    if not isinstance(result["limits"], dict):
        raise ValueError("health.limits must be a mapping")
    for key, bounds in result["limits"].items():
        if not isinstance(bounds, dict):
            raise ValueError(f"health.limits.{key} must be a mapping")
        for bound, value in bounds.items():
            if bound not in ("warn_above", "warn_below", "error_above", "error_below") or not numeric(value):
                raise ValueError(f"invalid health.limits.{key}.{bound}")
    return result


def telemetry_values(raw, *, temperature_supported=None):
    values = {}
    for key, (source, scale) in METRICS.items():
        value = raw.get(source)
        if numeric(value):
            values[key] = round(value * scale, 6)
        else:
            values[key] = None
            state = "unsupported" if key.startswith("temp.") and temperature_supported is False else "unavailable"
            values[f"health.metric.{key}.state"] = state
    voltage, current = values["supply.voltage_v"], values["supply.current_a"]
    power = voltage * current if numeric(voltage) and numeric(current) else None
    if numeric(power):
        values["supply.power_w"] = round(power, 6)
    else:
        values["supply.power_w"] = None
        values["health.metric.supply.power_w.state"] = "unavailable"
    return values


def status(component, level=OK, message="OK", values=None, hardware_id=""):
    return {"component": component, "level": level, "message": message,
            "hardware_id": hardware_id, "values": values or {}}


class HealthReporter:
    def __init__(self, instance, vehicle="", settings=None, *, clock=time.monotonic):
        self.instance, self.vehicle, self.settings = instance, vehicle, config(settings)
        self.clock = clock
        self.publisher_id = str(uuid.uuid4())
        self.sequence = 0
        self.samples = {}
        self.failures = {}
        self.alerts = {}
        self.next_cursor = None
        self.history_gap = False
        self.alert_generation = 0
        self.hardware_id = ""
        self.sensor_epoch = None
        self.temperature_supported = None
        self.packet_at = None
        self.packet_count = 0
        self.started = clock()
        self.active_since = None
        self.lock = threading.RLock()

    def record(self, group, reports):
        with self.lock:
            self.samples[group] = (self.clock(), str(uuid.uuid4()), reports)
            self.failures.pop(group, None)

    def fail(self, group, error):
        with self.lock:
            self.failures[group] = str(error)

    def identify(self, info):
        with self.lock:
            self.hardware_id = " ".join(str(info[k]) for k in ("prod_line", "prod_sn") if info.get(k))
            epoch = (info.get("prod_sn"), info.get("initialization_id"))
            if self.sensor_epoch is not None and epoch != self.sensor_epoch:
                self.next_cursor = None
                self.history_gap = True
                self.alert_generation += 1
            self.sensor_epoch = epoch
            # Older part numbers also use alphabetic revisions. Do not guess those.
            revision = str(info.get("prod_pn", "")).rsplit("-", 1)[-1] if "-" in str(info.get("prod_pn", "")) else ""
            self.temperature_supported = int(revision) >= 6 if revision.isdigit() else None

    def collect(self, sensor, changing):
        if changing.is_set():
            return None
        temperature = None
        try:
            raw = sensor.telemetry()
            if changing.is_set():
                return None
            values = telemetry_values(raw, temperature_supported=self.temperature_supported)
            temp = {k: v for k, v in values.items() if "temp." in k}
            power = {k: v for k, v in values.items() if "supply." in k}
            for group, data in (("temperature", temp), ("power", power)):
                missing = any(v == "unavailable" for k, v in data.items() if k.startswith("health.metric."))
                unsupported = any(v == "unsupported" for k, v in data.items() if k.startswith("health.metric."))
                self.record(group, [status(group, WARN if missing else OK,
                    "measurement unavailable" if missing else "unsupported by hardware" if unsupported else "OK", data)])
            temperature = values["temp.internal_c"]
        except Exception as error:
            self.fail("temperature", error)
            self.fail("power", error)
        if changing.is_set():
            return None
        try:
            raw_alerts = sensor.alerts()
            if not changing.is_set():
                self.update_alerts(raw_alerts)
        except Exception as error:
            self.fail("sensor alerts", error)
        return temperature

    def update_alerts(self, raw):
        active, events = raw.get("active"), raw.get("log", [])
        cursor = raw.get("next_cursor")
        if not isinstance(active, list) or not isinstance(events, list):
            raise ValueError("invalid alerts lists")
        for alert in active + events:
            if not isinstance(alert, dict) or not isinstance(alert.get("id"), str) or not alert["id"] or not isinstance(alert.get("level"), str):
                raise ValueError("invalid alert entry")
            if any(k in alert and not isinstance(alert[k], str) for k in ("msg", "msg_verbose", "category")):
                raise ValueError("invalid alert text")
            if "active" in alert and not isinstance(alert["active"], bool):
                raise ValueError("invalid alert active flag")
            if "cursor" in alert and (type(alert["cursor"]) is not int or alert["cursor"] < 0):
                raise ValueError("invalid alert event cursor")
        if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0):
            raise ValueError("invalid alert cursor")
        with self.lock:
            cursors = [e["cursor"] for e in events if isinstance(e.get("cursor"), int)]
            if self.next_cursor is not None and cursor is not None:
                if cursor < self.next_cursor or cursors and min(cursors) > self.next_cursor:
                    self.history_gap = True
                if cursor < self.next_cursor:
                    self.alert_generation += 1
            self.next_cursor = cursor
            current = {a["id"] for a in active}
            for a in events + active:
                self.alerts[a["id"]] = dict(a)
            reports = []
            worst = OK
            for code, a in sorted(self.alerts.items()):
                is_active = code in current
                raw_level = a["level"].upper()
                level = {"NOTICE": OK, "INFO": OK, "WARNING": WARN, "ERROR": ERROR}.get(raw_level, WARN) if is_active else OK
                worst = max(worst, level)
                reports.append(status(f"alert/{code}", level, a.get("msg", code) if is_active else "cleared", {
                    "alert.code": code, "alert.category": a.get("category", ""),
                    "alert.active": is_active, "alert.severity": raw_level,
                    "alert.detail": a.get("msg_verbose", ""),
                    "alert.sensor_time_ns": str(a.get("realtime", "")),
                    "alert.cursor": a.get("cursor"),
                }))
            reports.insert(0, status("sensor alerts", worst,
                f"{len(current)} active alert(s)", {"alerts.active": len(current), "alerts.history_gap": self.history_gap,
                "alerts.log": json.dumps(events[-32:], separators=(",", ":")),
                "alerts.epoch": f"{self.sensor_epoch}/{self.alert_generation}"}))
            self.record("sensor alerts", reports)

    def packet_seen(self):
        with self.lock:
            self.packet_at = self.clock()
            self.packet_count += 1

    def snapshot(self, operation=None, *, changing=False, stream_enabled=True):
        now = self.clock()
        with self.lock:
            self.sequence += 1
            operation = operation or {}
            mode = "transitioning" if changing else operation.get("state", "unknown")
            service_level = OK if mode in ("active", "standby") else WARN if changing else STALE
            if operation.get("error") and not changing:
                service_level = ERROR
            service = status("service", service_level, operation.get("error") or operation.get("detail", mode),
                             {"state": mode, "target": operation.get("target", ""),
                              "health.sample_age_s": max(0, now - operation.get("at", now)),
                              "health.sample_id": str(operation.get("at", ""))})
            groups = [("service", operation.get("at", self.started), "", [service])]
            if mode != "active":
                self.active_since = None
            elif self.active_since is None:
                self.active_since = now
            age = now - max(self.packet_at or self.started, self.active_since or self.started)
            stream_level = ERROR if mode == "active" and stream_enabled and age > 5 else OK
            stream = status("stream", stream_level,
                "standby" if mode == "standby" else "TLM disabled; data flow not monitored" if not stream_enabled else
                "no lidar telemetry packets" if stream_level == ERROR else "transitioning" if changing else "receiving" if self.packet_at else "waiting for packets",
                {"packets.received": self.packet_count, "packet_age_s": round(age, 3),
                 "health.availability": "unsupported" if not stream_enabled else "paused" if mode != "active" else "current"})
            groups.append(("stream", now, str(self.packet_count), [stream]))
            for group in ("temperature", "power", "sensor alerts"):
                at, sample_id, reports = self.samples.get(group, (self.started, "", [status(group, STALE, "waiting for observation")]))
                groups.append((group, at, sample_id, reports))
            output = []
            for group, at, sample_id, reports in groups:
                error = self.failures.get(group)
                available = "paused" if changing and group not in ("service", "stream") else "stale" if now - at > self.settings["stale_after_s"] else "retrying" if error else "current"
                for report in reports:
                    values = {"health.sample_age_s": round(max(0, now - at), 3),
                              "health.sample_id": sample_id, "health.availability": available, **report["values"]}
                    level, message = report["level"], report["message"]
                    for metric, bounds in self.settings["limits"].items():
                        value = values.get(metric)
                        if not numeric(value):
                            continue
                        for bound, severity in (("error_above", ERROR), ("error_below", ERROR), ("warn_above", WARN), ("warn_below", WARN)):
                            if bound in bounds and (value > bounds[bound] if bound.endswith("above") else value < bounds[bound]):
                                level = max(level, severity)
                                message += f"; {metric} {value:g} crossed {bound} {bounds[bound]:g}"
                                break
                    if available == "stale":
                        values["health.last_level"] = level
                        level = STALE
                    if level == STALE:
                        values["health.availability"] = "stale"
                    if error:
                        values["health.error"] = error
                    values.update({"health.instance": self.instance, "health.vehicle_id": self.vehicle,
                        "health.service": "ouster", "health.publisher_id": self.publisher_id,
                        "health.sequence": self.sequence, "health.stale_after_s": self.settings["stale_after_s"]})
                    values["health.publish_interval_s"] = self.settings["interval_s"]
                    output.append({"name": f"{self.instance}: {report['component']}", "level": level,
                                   "message": message, "hardware_id": report["hardware_id"] or self.hardware_id,
                                   "values": values})
            return output
