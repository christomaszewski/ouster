"""Behavioral regressions without ROS or a physical sensor.

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
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
        self.initializing_modes = {"NORMAL", "STANDBY"}
        self.telemetry = {"internal_temperature_deg_c": 45}
        self.alerts = {"active": [], "log": [], "next_cursor": 0}

    def __call__(self, request, timeout=None):
        path = urllib.parse.urlsplit(request.full_url)
        self.calls.append((request.get_method(), path.path, path.query))
        if path.path.endswith("/telemetry"):
            return HTTPResponse(self.telemetry)
        if path.path.endswith("/alerts"):
            return HTTPResponse(self.alerts)
        if path.path.endswith("/metadata/sensor_info"):
            if self.status_failures and self.active["operating_mode"] == "NORMAL":
                self.status_failures -= 1
                raise urllib.error.HTTPError(request.full_url, 503, "initializing", {}, None)
            status = "RUNNING" if self.active["operating_mode"] == "NORMAL" else "STANDBY"
            return HTTPResponse({"build_rev": self.version,
                                 "status": "INITIALIZING" if self.never_runs and
                                 self.active["operating_mode"] in self.initializing_modes else status})
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

    def test_shutdown_has_a_bounded_sensor_wait(self):
        clock = FakeClock()
        firmware = Firmware("v2.4.0")
        firmware.never_runs = True
        sensor = SensorHTTP("sensor", clock=clock, sleep=clock.sleep)
        with patch("urllib.request.urlopen", side_effect=firmware), \
                self.assertRaisesRegex(SensorError, "timed out"):
            sensor.ensure_mode("STANDBY", timeout=7)
        self.assertEqual(clock.now, 7)

    def test_shutdown_during_status_read_prevents_wakeup_write(self):
        stop = threading.Event()
        firmware = Firmware("v2.4.0")

        def read_then_stop(request, timeout=None):
            response = firmware(request, timeout)
            if "/cmd/get_config_param" in request.full_url:
                stop.set()
            return response

        with patch("urllib.request.urlopen", side_effect=read_then_stop), \
                self.assertRaisesRegex(SensorError, "cancelled"):
            SensorHTTP("sensor").ensure_mode("NORMAL", stop_event=stop)
        self.assertEqual(firmware.active["operating_mode"], "STANDBY")
        self.assertFalse(any("set_config" in path or "reinitialize" in path
                             for _, path, _ in firmware.calls))

    def test_shutdown_cancels_sensor_wait_promptly(self):
        stop = threading.Event()
        firmware = Firmware("v2.4.0")
        firmware.never_runs = True

        def stop_during_wait(seconds):
            stop.set()
            return True

        with patch("urllib.request.urlopen", side_effect=firmware), \
                patch.object(stop, "wait", side_effect=stop_during_wait), \
                self.assertRaisesRegex(SensorError, "cancelled"):
            SensorHTTP("sensor").ensure_mode("NORMAL", stop_event=stop)


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

    def ensure_mode(self, mode, **_):
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
        def ineffective_mode(_, **kwargs):
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

    def test_shutdown_parks_after_stopping_and_preserves_restart_target(self):
        self.controller.transition("active")
        self.events.clear()
        self.controller.shutdown("standby")
        self.assertEqual(self.events, ["driver:stop", "sensor:STANDBY"])
        self.assertEqual(self.target_file.read_text().strip(), "active")
        restarted = operational.Controller(self.sensor, self.driver, "standby", self.target_file)
        restarted.transition()
        self.assertEqual(restarted.state()["state"], "active")

    def test_shutdown_unchanged_does_not_write_sensor(self):
        self.controller.transition("active")
        self.events.clear()
        self.controller.shutdown("unchanged")
        self.assertEqual(self.events, ["driver:stop"])
        self.assertEqual(self.sensor.mode, "NORMAL")

    def test_shutdown_does_not_claim_standby_if_sensor_is_unreachable(self):
        self.controller.transition("active")
        self.sensor.fail = True
        with self.assertLogs("ouster-state", "ERROR") as logs, \
                self.assertRaisesRegex(OSError, "disconnected"):
            self.controller.shutdown("standby")
        self.assertIn("NOT confirmed", logs.output[0])
        self.assertEqual(self.target_file.read_text().strip(), "active")

    def test_unstoppable_driver_prevents_shutdown_sensor_write(self):
        self.driver.process = object()
        self.driver.stop_fails = True
        with self.assertLogs("ouster-state", "ERROR"), \
                self.assertRaisesRegex(RuntimeError, "would not stop"):
            self.controller.shutdown("standby")
        self.assertEqual(self.events, ["driver:stop"])

    def test_commands_after_shutdown_cannot_save_or_wake(self):
        self.controller.transition("standby")
        self.controller.shutdown("standby")
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, "stopping"):
            self.controller.transition("active")
        self.assertEqual(self.target_file.read_text().strip(), "standby")
        self.assertEqual(self.events, [])

    def test_command_waiting_on_lock_cannot_restart_after_shutdown(self):
        self.controller.transition("standby")
        self.controller.lock.acquire()
        errors = []

        def activate():
            try:
                self.controller.transition("active")
            except RuntimeError as error:
                errors.append(str(error))

        worker = threading.Thread(target=activate)
        worker.start()
        self.controller.stop_event.set()
        self.controller.lock.release()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["supervisor stopping"])
        self.assertEqual(self.target_file.read_text().strip(), "standby")
        self.assertNotIn("driver:start", self.events)

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
    def test_persistent_lifecycle_drains_before_signalling_without_cli(self):
        from unittest.mock import Mock
        for state, expected in (("active", ["deactivate", "cleanup"]),
                                ("inactive", ["cleanup"]),
                                ("unconfigured", [])):
            with self.subTest(state=state):
                driver = operational.Driver("unused", "/test")
                driver.process = Mock(pid=123)
                driver.process.poll.return_value = None
                driver.lifecycle_client = Mock()
                driver.lifecycle_client.state.return_value = state
                calls = []
                driver.lifecycle_client.transition.side_effect = calls.append

                def signal_finished_group(*_):
                    calls.append("signal")
                    raise ProcessLookupError

                with patch.object(operational.subprocess, "run") as cli, \
                        patch.object(operational.os, "killpg", side_effect=signal_finished_group):
                    driver.stop()
                self.assertEqual(calls, expected + ["signal"])
                cli.assert_not_called()
                self.assertIsNone(driver.process)

    def test_persistent_lifecycle_failure_still_stops_group_without_cli_retry(self):
        from unittest.mock import Mock
        for failure in (TimeoutError("no reply"), RuntimeError("transition rejected")):
            with self.subTest(failure=failure):
                driver = operational.Driver("unused", "/test")
                driver.process = Mock(pid=123)
                driver.process.poll.return_value = None
                driver.lifecycle_client = Mock()
                driver.lifecycle_client.state.return_value = "active"
                driver.lifecycle_client.transition.side_effect = failure
                with patch.object(operational.subprocess, "run") as cli, \
                        patch.object(operational.os, "killpg", side_effect=ProcessLookupError()) as kill, \
                        self.assertLogs("ouster-state", "WARNING"):
                    driver.stop()
                kill.assert_called_once_with(123, signal.SIGINT)
                cli.assert_not_called()
                self.assertIsNone(driver.process)

    def test_persistent_lifecycle_probe_failure_reports_unreachable(self):
        from unittest.mock import Mock
        driver = operational.Driver("unused", "/test")
        driver.process = Mock()
        driver.process.poll.return_value = None
        driver.lifecycle_client = Mock()
        driver.lifecycle_client.state.side_effect = TimeoutError("no reply")
        with patch.object(operational.subprocess, "run") as cli:
            self.assertEqual(driver.lifecycle(), "unreachable")
        cli.assert_not_called()

    def test_stop_drains_reader_and_scan_threads_before_signalling(self):
        from unittest.mock import Mock
        driver = operational.Driver("unused", "/test")
        driver.process = Mock(pid=123)
        calls = []

        def signal_finished_group(*_):
            calls.append("signal")
            raise ProcessLookupError

        with patch.object(driver, "running", return_value=True), \
                patch.object(driver, "lifecycle", return_value="active"), \
                patch.object(operational.subprocess, "run",
                             side_effect=lambda cmd, **_: calls.append(cmd[-1])), \
                patch.object(operational.os, "killpg", side_effect=signal_finished_group):
            driver.stop()
        self.assertEqual(calls, ["deactivate", "cleanup", "signal"])
        self.assertIsNone(driver.process)

    def test_failed_lifecycle_cleanup_still_stops_process_group(self):
        from unittest.mock import Mock
        driver = operational.Driver("unused", "/test")
        driver.process = Mock(pid=123)
        with patch.object(driver, "running", return_value=True), \
                patch.object(driver, "lifecycle", return_value="active"), \
                patch.object(operational.subprocess, "run", side_effect=
                             subprocess.TimeoutExpired("ros2", 5)), \
                patch.object(operational.os, "killpg", side_effect=ProcessLookupError()) as kill, \
                self.assertLogs("ouster-state", "WARNING"):
            driver.stop()
        kill.assert_called_once_with(123, signal.SIGINT)
        self.assertIsNone(driver.process)

    def test_lifecycle_ignores_zenoh_warnings_on_stdout(self):
        driver = operational.Driver("unused", "/test")
        warning = '\x1b[2m2026-10-07T18:25:19Z\x1b[0m WARN Watchdog: priority denied\n'
        for output, expected in (
            ("active [3]\n", "active"),
            (warning + "active [3]\n", "active"),
            ("active [3]\n" + warning, "active"),
            (warning + "\x1b[32minactive [2]\x1b[0m\n", "inactive"),
            (warning + "unknown [0]\n", "unknown"),
            (warning + "active: waiting for response\n", "unreachable"),
            ("", "unreachable"),
        ):
            with self.subTest(output=output), patch.object(driver, "running", return_value=True), \
                    patch.object(operational.subprocess, "run", return_value=
                                 subprocess.CompletedProcess([], 0, stdout=output)):
                self.assertEqual(driver.lifecycle(), expected)

    def test_lifecycle_failed_probe_cannot_report_active(self):
        driver = operational.Driver("unused", "/test")
        for error in (
            subprocess.CalledProcessError(1, "ros2", output="active [3]\n"),
            subprocess.TimeoutExpired("ros2", 5, output="active [3]\n"),
        ):
            with self.subTest(error=error), patch.object(driver, "running", return_value=True), \
                    patch.object(operational.subprocess, "run", side_effect=error):
                self.assertEqual(driver.lifecycle(), "unreachable")

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
