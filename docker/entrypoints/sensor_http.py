"""Firmware-aware, non-persistent sensor mode changes (FW 2.3+)."""

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class SensorError(RuntimeError):
    pass


class SensorHTTP:
    def __init__(self, hostname, *, clock=time.monotonic, sleep=time.sleep):
        self.base = f"http://{hostname}/api/v1/sensor"
        self.clock = clock
        self.sleep = sleep

    def request(self, path, body=None, timeout=5):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={} if body is None else {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode()
        except urllib.error.HTTPError as error:
            error.close()
            raise

    @staticmethod
    def transient(error):
        if isinstance(error, urllib.error.HTTPError):
            return error.code in (408, 429, 500, 502, 503, 504)
        return isinstance(error, (urllib.error.URLError, OSError, ValueError))

    @staticmethod
    def version(info):
        match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", info.get("build_rev", ""))
        if not match:
            raise SensorError(f"unrecognized sensor firmware: {info.get('build_rev')!r}")
        return tuple(map(int, match.groups()))

    def snapshot(self, timeout=3):
        info = json.loads(self.request("/metadata/sensor_info", timeout=timeout))
        legacy = self.version(info) < (3, 1, 0)
        path = "/cmd/get_config_param?args=active" if legacy else "/config?staging=false"
        config = json.loads(self.request(path, timeout=timeout))
        if config.get("operating_mode") not in ("NORMAL", "STANDBY"):
            raise SensorError("sensor active config has no valid operating_mode")
        return info, config

    def temperature(self):
        """Read internal temperature in Celsius; older hardware may omit it."""
        telemetry = json.loads(self.request("/telemetry", timeout=2))
        if not isinstance(telemetry, dict):
            raise SensorError("invalid sensor telemetry response")
        value = telemetry.get("internal_temperature_deg_c")
        if value is None:
            raise SensorError("internal temperature unavailable (requires Rev 06 or newer hardware)")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SensorError("invalid internal_temperature_deg_c in sensor telemetry")
        return float(value)

    def read_until(self, deadline):
        while self.clock() < deadline:
            try:
                return self.snapshot(timeout=max(0.1, min(3, (deadline - self.clock()) / 2)))
            except Exception as error:
                if not self.transient(error):
                    raise
                self.sleep(min(2, max(0, deadline - self.clock())))
        raise SensorError("timed out reading sensor status/config")

    def ensure_mode(self, target):
        expected = {"NORMAL": "RUNNING", "STANDBY": "STANDBY"}[target]
        deadline = self.clock() + (120 if target == "NORMAL" else 60)
        info, config = self.read_until(deadline)
        if config["operating_mode"] != target:
            # Reset staging from active config, so applying our mode cannot also
            # apply unrelated values left staged by another client.
            config = dict(config, operating_mode=target)
            if "auto_start_flag" in config:
                config["auto_start_flag"] = int(target == "NORMAL")
            legacy = self.version(info) < (3, 1, 0)
            if legacy:
                value = urllib.parse.quote(json.dumps(config), safe="")
                reply = self.request(f"/cmd/set_config_param?args=.+{value}")
                if json.loads(reply) != "set_config_param":
                    raise SensorError(f"sensor rejected staged config: {reply}")
            else:
                self.request("/config?staging=true&reinit=false&persist=false", config)
            try:
                if legacy:
                    self.request("/cmd/reinitialize")
                else:
                    self.request("/config?staging=true&reinit=true&persist=false", {})
            except Exception as error:
                # A lost reinit response is ambiguous. Observe the result; do
                # not repeatedly reinitialize a sensor that is spinning up.
                if not self.transient(error):
                    raise

        while self.clock() < deadline:
            info, config = self.read_until(deadline)
            status = info.get("status", "unknown")
            if config["operating_mode"] == target and status == expected:
                return
            if status == "ERROR":
                raise SensorError("sensor reports ERROR")
            self.sleep(min(2, max(0, deadline - self.clock())))
        raise SensorError(f"timed out waiting for sensor {target}/{expected}")
