"""Real HTTP/socket/process integration, plus ROS temperature when installed."""

import http.server
import importlib.util
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

from test_operational_state import Firmware, ROOT
import operational_state as operational

HAS_ROS = importlib.util.find_spec("rclpy") is not None


class Request:
    def __init__(self, url):
        self.full_url = url
        self.data = None

    def get_method(self):
        return "GET"


class HTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.lock:
            response = self.server.firmware(Request("http://localhost" + self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response.data)))
        self.end_headers()
        self.wfile.write(response.data)

    def log_message(self, *_):
        pass


class IntegrationTests(unittest.TestCase):
    def test_park_wake_restart_and_sensor_power_cycle(self):
        if os.environ.get("OUSTER_TEST_ENTRYPOINT_DIR"):
            self.assertTrue(HAS_ROS, "packaged runtime must include rclpy for temperature publishing")
        with tempfile.TemporaryDirectory(prefix="ouster-e2e-", dir="/tmp") as td:
            root = Path(td)
            firmware = Firmware("v2.4.0")
            with http.server.ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler) as sensor:
                sensor.firmware = firmware
                sensor.lock = threading.Lock()
                http_thread = threading.Thread(target=sensor.serve_forever)
                http_thread.start()
                bin_dir = root / "bin"
                bin_dir.mkdir()
                ros = bin_dir / "ros2"
                # Simulate a driver recovery task that repeatedly sets NORMAL.
                # It MUST be dead before the controller writes STANDBY.
                ros.write_text(f'''#!{sys.executable}
import importlib.util, os, signal, sys, time, urllib.request
if sys.argv[1:3] == ['lifecycle', 'get']:
    with open(os.environ['FAKE_DRIVER_CLI'], 'a') as stream:
        stream.write('get\\n')
    print('2026-10-07T18:25:19Z WARN Watchdog Confirmator: priority denied')
    print('active [3]')
    print('2026-10-07T18:25:19Z WARN Watchdog Validator: priority denied')
    sys.exit(0)
if sys.argv[1:3] == ['lifecycle', 'set']:
    with open(os.environ['FAKE_DRIVER_CLI'], 'a') as stream:
        stream.write('set\\n')
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
with open(os.environ['FAKE_DRIVER_STARTS'], 'a') as stream:
    stream.write(str(os.getpid()) + '\\n')
def write_normal():
    urllib.request.urlopen(os.environ['FAKE_SENSOR'] + '/api/v1/sensor/cmd/set_config_param?args=.+%7B%22operating_mode%22%3A%22NORMAL%22%2C%22udp_port_lidar%22%3A7502%7D', timeout=1).close()
    urllib.request.urlopen(os.environ['FAKE_SENSOR'] + '/api/v1/sensor/cmd/reinitialize', timeout=1).close()
if importlib.util.find_spec('rclpy'):
    import rclpy
    from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
    class TestDriver(LifecycleNode):
        def on_activate(self, state):
            self.writer = self.create_timer(0.2, write_normal)
            return TransitionCallbackReturn.SUCCESS
        def on_deactivate(self, state):
            self.destroy_timer(self.writer)
            with open(os.environ['FAKE_DRIVER_TRANSITIONS'], 'a') as stream:
                stream.write('deactivate\\n')
            return TransitionCallbackReturn.SUCCESS
        def on_cleanup(self, state):
            with open(os.environ['FAKE_DRIVER_TRANSITIONS'], 'a') as stream:
                stream.write('cleanup\\n')
            return TransitionCallbackReturn.SUCCESS
    rclpy.init(args=[])
    node = TestDriver('os_driver', namespace=os.environ['OUSTER_NAMESPACE'])
    node.trigger_configure()
    node.trigger_activate()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
else:
    while True:
        write_normal()
        time.sleep(0.2)
''')
                ros.chmod(0o755)
                params = root / "params.yaml"
                params.write_text(f"/**:\n  ros__parameters:\n    sensor_hostname: 127.0.0.1:{sensor.server_port}\n"
                                  "    sensor_frame: test_sensor_frame\n")
                starts = root / "starts"
                socket_path = str(root / "state.sock")
                env = dict(os.environ, PATH=str(bin_dir) + ":" + os.environ['PATH'],
                           OUSTER_PARAMS_FILE=str(params), OUSTER_STATE_SOCKET=socket_path,
                           OUSTER_TARGET_FILE=str(root / "target"), OUSTER_TARGET_STATE="standby",
                           OUSTER_NAMESPACE="test_ouster",
                           FAKE_DRIVER_CLI=str(root / "cli"),
                           FAKE_DRIVER_TRANSITIONS=str(root / "transitions"),
                           FAKE_DRIVER_STARTS=str(starts), FAKE_SENSOR=f"http://127.0.0.1:{sensor.server_port}")
                log = (root / "supervisor.log").open("w+")
                process = None
                entrypoints = Path(os.environ.get("OUSTER_TEST_ENTRYPOINT_DIR", ROOT / "docker/entrypoints"))

                def start():
                    command = [sys.executable, str(entrypoints / "operational_state.py"), "serve"]
                    if not HAS_ROS:
                        # Exercise the same supervisor without the ROS adapter on
                        # bare CI/host Python. Runtime-image CI uses real rclpy.
                        command = [sys.executable, "-c",
                                   "import sys; from contextlib import nullcontext; "
                                   "sys.path.insert(0, sys.argv[1]); import operational_state; "
                                   "operational_state.serve(monitor=lambda *_: nullcontext())",
                                   str(entrypoints)]
                    return subprocess.Popen(command, env=env, stdout=log, stderr=log)

                def wait_state(expected):
                    deadline = time.monotonic() + 15
                    last = None
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "supervisor exited")
                        try:
                            last = operational.client("state", output=False)
                            if last["state"] == expected:
                                return last
                        except (OSError, ValueError):
                            pass
                        time.sleep(0.1)
                    self.fail(f"did not reach {expected}: {last}")

                listener = context = executor = None
                readings = []
                diagnostics = []
                if HAS_ROS:
                    import rclpy
                    from rclpy.context import Context
                    from rclpy.executors import SingleThreadedExecutor
                    from rclpy.signals import SignalHandlerOptions
                    from sensor_msgs.msg import Temperature
                    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
                    context = Context()
                    rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
                    listener = rclpy.create_node("temperature_test", context=context)
                    listener.create_subscription(Temperature, "/test_ouster/temperature", readings.append, 1)
                    listener.create_subscription(DiagnosticArray, "/diagnostics", diagnostics.append, 10)
                    executor = SingleThreadedExecutor(context=context)
                    executor.add_node(listener)

                def wait_temperature(value):
                    if listener is None:
                        return
                    readings.clear()
                    with sensor.lock:
                        firmware.telemetry["internal_temperature_deg_c"] = value
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        executor.spin_once(timeout_sec=0.1)
                        if readings and readings[-1].temperature == value:
                            message = readings[-1]
                            self.assertEqual(message.header.frame_id, "test_sensor_frame")
                            self.assertEqual(message.variance, 0.0)
                            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
                            self.assertLess(abs(time.time() - stamp), 3, "stamp must be host receipt time")
                            return
                    log.flush()
                    log.seek(0)
                    self.fail(f"no temperature {value}; received: {readings}; supervisor: {log.read()}")

                def wait_diagnostics(predicate):
                    if listener is None:
                        return
                    diagnostics.clear()
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        executor.spin_once(timeout_sec=0.1)
                        if diagnostics and predicate({s.name.split(": ", 1)[1]: s for s in diagnostics[-1].status}):
                            return
                    self.fail(f"no expected diagnostics; received: {diagnostics[-1:]}")

                try:
                    with patch.object(operational, "SOCKET", socket_path):
                        process = start()
                        wait_state("standby")
                        wait_temperature(45.0)
                        with sensor.lock:
                            firmware.telemetry.update(input_voltage_mv=23606, input_current_ma=758)
                            firmware.alerts = {"active": [{"id": "POWER_LOW", "level": "ERROR", "msg": "input low"}], "log": [], "next_cursor": 1}
                        wait_diagnostics(lambda rows: rows.get("alert/POWER_LOW") is not None and rows["alert/POWER_LOW"].level == DiagnosticStatus.ERROR and
                                         dict((v.key, v.value) for v in rows["power"].values).get("supply.power_w") == "17.893348")
                        with sensor.lock:
                            firmware.alerts["active"] = []
                        wait_diagnostics(lambda rows: rows.get("alert/POWER_LOW") is not None and rows["alert/POWER_LOW"].level == DiagnosticStatus.OK)
                        self.assertFalse(starts.exists(), "standby boot launched the driver")
                        health = subprocess.run([str(entrypoints / "healthcheck.sh")], env=env,
                                                capture_output=True, text=True, timeout=20)
                        self.assertEqual(health.returncode, 0, health.stderr)
                        self.assertEqual(health.stdout, "")
                        state = subprocess.run([str(entrypoints / "statectl.sh"), "state"], env=env,
                                               capture_output=True, text=True, timeout=20)
                        self.assertEqual(state.returncode, 0, state.stderr)
                        self.assertEqual(json.loads(state.stdout)["state"], "standby")
                        for cycle in range(2):
                            operational.client("activate")
                            wait_state("active")
                            wait_temperature(46.5 + cycle)
                            operational.client("standby")
                            wait_state("standby")
                            wait_temperature(40.5 + cycle)
                        time.sleep(0.5)
                        self.assertEqual(firmware.active["operating_mode"], "STANDBY")
                        self.assertEqual(len(starts.read_text().splitlines()), 2)

                        # Restart with an active default: the accepted runtime
                        # standby request must win within the same container.
                        process.terminate()
                        process.wait(timeout=10)
                        env["OUSTER_TARGET_STATE"] = "active"
                        process = start()
                        wait_state("standby")
                        wait_temperature(39.0)
                        self.assertEqual(len(starts.read_text().splitlines()), 2)

                        # Simulate power-up from a different persisted mode.
                        with sensor.lock:
                            firmware.active["operating_mode"] = "NORMAL"
                        wait_state("standby")
                        self.assertEqual(len(starts.read_text().splitlines()), 2)

                        # SIGTERM (Docker stop / rig down) must physically park
                        # an active sensor, after killing its reconnect writer.
                        operational.client("activate")
                        wait_state("active")
                        if HAS_ROS:
                            transitions_before = (root / "transitions").read_text()
                        shutdown_started = time.monotonic()
                        process.terminate()
                        self.assertEqual(process.wait(timeout=10), 0)
                        if HAS_ROS:
                            elapsed = time.monotonic() - shutdown_started
                            print(f"simulated sensor shutdown with ROS lifecycle: {elapsed:.2f}s")
                            self.assertEqual((root / "transitions").read_text(),
                                             transitions_before + "deactivate\ncleanup\n")
                            self.assertFalse((root / "cli").exists(),
                                             "persistent lifecycle calls fell back to CLI")
                        time.sleep(0.3)
                        self.assertEqual(firmware.active["operating_mode"], "STANDBY")
                        self.assertEqual((root / "target").read_text().strip(), "active")

                        # Parking on shutdown must not replace the saved active
                        # target. The opt-out stops ROS but leaves NORMAL intact.
                        env["OUSTER_TARGET_STATE"] = "standby"
                        env["OUSTER_SHUTDOWN_STATE"] = "unchanged"
                        process = start()
                        wait_state("active")
                        process.send_signal(signal.SIGINT)
                        self.assertEqual(process.wait(timeout=10), 0)
                        self.assertEqual(firmware.active["operating_mode"], "NORMAL")

                        # Shutdown during a stuck wake-up cancels the NORMAL
                        # transition, then completes a separate STANDBY request.
                        env["OUSTER_SHUTDOWN_STATE"] = "standby"
                        with sensor.lock:
                            firmware.active["operating_mode"] = "STANDBY"
                            firmware.never_runs = True
                            firmware.initializing_modes = {"NORMAL"}
                        previous_starts = starts.read_text()
                        process = start()
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            with sensor.lock:
                                waking = firmware.active["operating_mode"] == "NORMAL"
                            if waking:
                                break
                            time.sleep(0.1)
                        self.assertTrue(waking, "supervisor did not begin waking sensor")
                        process.terminate()
                        self.assertEqual(process.wait(timeout=10), 0)
                        self.assertEqual(firmware.active["operating_mode"], "STANDBY")
                        self.assertEqual(starts.read_text(), previous_starts,
                                         "driver started while sensor was initializing")
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    # Clean up even if the assertion or supervisor failed.
                    if starts.exists():
                        for pid in starts.read_text().splitlines():
                            try:
                                os.killpg(int(pid), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    sensor.shutdown()
                    http_thread.join()
                    log.close()
                    if listener is not None:
                        executor.shutdown()
                        listener.destroy_node()
                        context.try_shutdown()


if __name__ == "__main__":
    unittest.main()
