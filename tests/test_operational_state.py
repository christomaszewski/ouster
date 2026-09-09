"""Behavioral regressions without ROS or a physical sensor.

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
from sensor_http import SensorHTTP, SensorError
import operational_state as operational


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class HTTPResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.data


class Firmware:
    """Model active/staged/persisted config separately, including uncertain reinit."""
    def __init__(self, version):
        self.version = version
        self.active = {"operating_mode": "STANDBY", "udp_port_lidar": 7502}
        self.persisted = dict(self.active)
        self.staged = dict(self.active, udp_port_lidar=9999)
        self.calls = []
        self.status_failures = 0
        self.lose_reinit_response = False
        self.never_runs = False

    def __call__(self, request, timeout=None):
        path = urllib.parse.urlsplit(request.full_url)
        self.calls.append((request.get_method(), path.path, path.query))
        if path.path.endswith("/metadata/sensor_info"):
            if self.status_failures and self.active["operating_mode"] == "NORMAL":
                self.status_failures -= 1
                raise urllib.error.HTTPError(request.full_url, 503, "initializing", {}, None)
            status = "RUNNING" if self.active["operating_mode"] == "NORMAL" else "STANDBY"
            return HTTPResponse({"build_rev": self.version,
                                 "status": "INITIALIZING" if self.never_runs else status})
        if path.path.endswith("/cmd/get_config_param"):
            return HTTPResponse(self.active)
        if path.path.endswith("/cmd/set_config_param"):
            args = urllib.parse.parse_qs(path.query)["args"][0]
            self.staged = json.loads(args.split(" ", 1)[1])
            return HTTPResponse("set_config_param")
        if path.path.endswith("/config") and request.get_method() == "GET":
            return HTTPResponse(self.active)
        if path.path.endswith("/config") and "reinit=false" in path.query:
            self.staged = json.loads(request.data)
            return HTTPResponse({})
        if path.path.endswith("/cmd/reinitialize") or "reinit=true" in path.query:
            self.active = dict(self.staged)
            if self.lose_reinit_response:
                raise urllib.error.URLError("connection reset after applying config")
            return HTTPResponse({})
        raise AssertionError(f"unexpected sensor request: {request.full_url}")


class SensorTests(unittest.TestCase):
    def run_mode(self, firmware, target="NORMAL"):
        clock = FakeClock()
        sensor = SensorHTTP("test-sensor", clock=clock, sleep=clock.sleep)
        with patch("urllib.request.urlopen", side_effect=firmware):
            sensor.ensure_mode(target)
        return clock

    def test_fw24_uses_legacy_commands_and_never_persists(self):
        fw = Firmware("v2.4.0")
        self.run_mode(fw)
        self.assertEqual(fw.active, {"operating_mode": "NORMAL", "udp_port_lidar": 7502})
        self.assertEqual(fw.persisted["operating_mode"], "STANDBY")
        self.assertTrue(all(method == "GET" for method, _, _ in fw.calls))
        self.assertFalse(any(path.endswith("/config") for _, path, _ in fw.calls))
        self.assertTrue(any(path.endswith("/cmd/reinitialize") for _, path, _ in fw.calls))

    def test_fw31_uses_explicit_nonpersistent_staging(self):
        fw = Firmware("v3.1.0")
        self.run_mode(fw)
        writes = [query for method, _, query in fw.calls if method == "POST"]
        self.assertEqual(writes, ["staging=true&reinit=false&persist=false",
                                  "staging=true&reinit=true&persist=false"])
        self.assertEqual(fw.active["udp_port_lidar"], 7502)

    def test_transient_spinup_failures_are_retried(self):
        fw = Firmware("v2.4.0")
        fw.status_failures = 3
        clock = self.run_mode(fw)
        self.assertGreaterEqual(clock.now, 6)

    def test_lost_reinit_response_does_not_trigger_second_reinit(self):
        fw = Firmware("v2.4.0")
        fw.lose_reinit_response = True
        self.run_mode(fw)
        self.assertEqual(sum(path.endswith("/cmd/reinitialize") for _, path, _ in fw.calls), 1)

    def test_already_standby_is_read_only(self):
        fw = Firmware("v2.4.0")
        self.run_mode(fw, "STANDBY")
        self.assertFalse(any("set_config" in path or "reinitialize" in path for _, path, _ in fw.calls))

    def test_initializing_sensor_has_bounded_wait(self):
        fw = Firmware("v2.4.0")
        fw.never_runs = True
        with self.assertRaisesRegex(SensorError, "timed out"):
            self.run_mode(fw)

    def test_http_400_is_not_misreported_as_unreachable_or_retried(self):
        fw = Firmware("v2.4.0")
        def reject(request, timeout=None):
            if "/cmd/set_config_param" in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 400, "bad config", {}, None)
            return fw(request, timeout)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.run_mode(reject)
        self.assertEqual(caught.exception.code, 400)


class FakeDriver:
    def __init__(self, events):
        self.events = events
        self.process = None
        self.lc = "unreachable"
        self.stop_fails = False

    def running(self):
        return self.process is not None

    def lifecycle(self):
        return self.lc if self.running() else "absent"

    def stop(self):
        self.events.append("driver:stop")
        if self.stop_fails:
            raise RuntimeError("process group would not stop")
        self.process = None

    def start(self):
        self.events.append("driver:start")
        self.process = object()
        self.lc = "active"


class FakeSensor:
    def __init__(self, events):
        self.events = events
        self.mode = "STANDBY"
        self.status = "STANDBY"
        self.fail = False

    def snapshot(self):
        if self.fail:
            raise OSError("sensor disconnected")
        return {"status": self.status}, {"operating_mode": self.mode}

    def ensure_mode(self, mode):
        self.events.append("sensor:" + mode)
        if self.fail:
            raise OSError("sensor disconnected")
        self.mode = mode
        self.status = "RUNNING" if mode == "NORMAL" else "STANDBY"


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = []
        self.sensor = FakeSensor(self.events)
        self.driver = FakeDriver(self.events)
        self.target_file = Path(self.temp.name) / "target"
        self.controller = operational.Controller(self.sensor, self.driver, "standby", self.target_file)

    def test_standby_boot_never_starts_or_wakes_driver(self):
        self.controller.transition()
        self.assertEqual(self.events, [])
        self.assertEqual(self.controller.state()["state"], "standby")

    def test_activate_wakes_before_start_even_if_old_ros_is_unreachable(self):
        self.driver.process = object()
        self.controller.transition("active")
        self.assertEqual(self.events, ["driver:stop", "sensor:NORMAL", "driver:start"])
        self.assertEqual(self.controller.state()["state"], "active")

    def test_standby_stops_recovery_even_when_lifecycle_is_inactive(self):
        self.driver.process = object()
        self.driver.lc = "inactive"
        self.sensor.mode, self.sensor.status = "NORMAL", "RUNNING"
        self.controller.transition("standby")
        self.assertEqual(self.events, ["driver:stop", "sensor:STANDBY"])
        self.assertEqual(self.controller.state()["state"], "standby")

    def test_unstoppable_driver_prevents_sensor_write(self):
        self.driver.process = object()
        self.driver.stop_fails = True
        with self.assertRaisesRegex(RuntimeError, "would not stop"):
            self.controller.transition("standby")
        self.assertFalse(any(e.startswith("sensor:") for e in self.events))

    def test_inactive_ros_and_spinning_sensor_are_not_standby(self):
        self.driver.process = object()
        self.driver.lc = "inactive"
        self.sensor.mode, self.sensor.status = "NORMAL", "RUNNING"
        self.assertEqual(self.controller.state()["state"], "transitioning")

    def test_failed_standby_remains_unhealthy_and_retains_target(self):
        self.driver.process = object()
        self.sensor.fail = True
        with self.assertRaises(OSError):
            self.controller.transition("standby")
        self.assertEqual(self.controller.state()["state"], "transitioning")
        self.assertEqual(self.target_file.read_text().strip(), "standby")

    def test_runtime_standby_survives_container_restart(self):
        self.controller.transition("standby")
        restarted = operational.Controller(self.sensor, self.driver, "active", self.target_file)
        restarted.transition()
        self.assertEqual(restarted.state()["state"], "standby")
        self.assertNotIn("driver:start", self.events)

    def test_power_cycle_to_normal_is_reparked(self):
        self.sensor.mode, self.sensor.status = "NORMAL", "RUNNING"
        self.controller.transition()
        self.assertEqual(self.controller.state()["state"], "standby")

    def test_state_during_transition_is_immediate_and_truthful(self):
        self.controller.changing.set()
        self.sensor.fail = True
        self.assertEqual(self.controller.state()["state"], "transitioning")

    def test_monitor_observation_does_not_make_standby_unhealthy(self):
        with self.controller.lock:
            self.assertEqual(self.controller.state()["state"], "standby")

    def test_final_sensor_mismatch_cannot_report_success(self):
        def ineffective_mode(_):
            self.sensor.mode, self.sensor.status = "NORMAL", "RUNNING"
        self.sensor.mode, self.sensor.status = "NORMAL", "RUNNING"
        self.sensor.ensure_mode = ineffective_mode
        with self.assertRaisesRegex(RuntimeError, "did not settle"):
            self.controller.transition("standby")

    def test_activation_is_idempotent(self):
        self.controller.transition("active")
        self.events.clear()
        self.controller.transition("active")
        self.assertEqual(self.events, [])

    def test_socket_commands_and_health(self):
        path = str(Path(self.temp.name) / "state.sock")
        with operational.Server(path, operational.Handler) as server:
            server.controller = self.controller
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with patch.object(operational, "SOCKET", path):
                    operational.client("activate")
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        operational.client("state")
                    self.assertEqual(json.loads(output.getvalue())["state"], "active")
                    operational.client("standby")
                    self.assertEqual(operational.client("state", output=False)["state"], "standby")
            finally:
                server.shutdown()
                worker.join()


class ProcessTests(unittest.TestCase):
    def test_stopping_driver_terminates_its_child_process_group(self):
        driver = operational.Driver("unused", "/test")
        # No ROS or hardware. Exercise real process-group ownership and cleanup.
        driver.command = [sys.executable, "-c",
                          "import subprocess,sys,time,signal; "
                          "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                          "p=subprocess.Popen([sys.executable,'-c',"
                          "'import signal,time; signal.signal(signal.SIGINT, lambda *_: exit(0)); time.sleep(60)']); "
                          "p.wait()"]
        driver.start()
        pgid = driver.process.pid
        try:
            time.sleep(0.15)
            driver.stop()
            with self.assertRaises(ProcessLookupError):
                os.killpg(pgid, 0)
        finally:
            if driver.process is not None:
                os.killpg(pgid, signal.SIGKILL)
                driver.process.wait()


if __name__ == "__main__":
    unittest.main()
