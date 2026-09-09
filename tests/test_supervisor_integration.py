"""Real HTTP/socket/subprocess integration; no ROS installation or hardware."""

import http.server
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
import os, signal, sys, time, urllib.request
if sys.argv[1:3] == ['lifecycle', 'get']:
    print('active [3]')
    sys.exit(0)
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
with open(os.environ['FAKE_DRIVER_STARTS'], 'a') as stream:
    stream.write(str(os.getpid()) + '\\n')
while True:
    urllib.request.urlopen(os.environ['FAKE_SENSOR'] + '/api/v1/sensor/cmd/set_config_param?args=.+%7B%22operating_mode%22%3A%22NORMAL%22%2C%22udp_port_lidar%22%3A7502%7D', timeout=1).close()
    urllib.request.urlopen(os.environ['FAKE_SENSOR'] + '/api/v1/sensor/cmd/reinitialize', timeout=1).close()
    time.sleep(0.2)
''')
                ros.chmod(0o755)
                params = root / "params.yaml"
                params.write_text(f"/**:\n  ros__parameters:\n    sensor_hostname: 127.0.0.1:{sensor.server_port}\n")
                starts = root / "starts"
                socket_path = str(root / "state.sock")
                env = dict(os.environ, PATH=str(bin_dir) + ":" + os.environ['PATH'],
                           OUSTER_PARAMS_FILE=str(params), OUSTER_STATE_SOCKET=socket_path,
                           OUSTER_TARGET_FILE=str(root / "target"), OUSTER_TARGET_STATE="standby",
                           FAKE_DRIVER_STARTS=str(starts), FAKE_SENSOR=f"http://127.0.0.1:{sensor.server_port}")
                log = (root / "supervisor.log").open("w+")
                process = None
                entrypoints = Path(os.environ.get("OUSTER_TEST_ENTRYPOINT_DIR", ROOT / "docker/entrypoints"))

                def start():
                    return subprocess.Popen([sys.executable, str(entrypoints / "operational_state.py"), "serve"],
                                            env=env, stdout=log, stderr=log)

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

                try:
                    with patch.object(operational, "SOCKET", socket_path):
                        process = start()
                        wait_state("standby")
                        self.assertFalse(starts.exists(), "standby boot launched the driver")
                        health = subprocess.run([str(entrypoints / "healthcheck.sh")], env=env,
                                                capture_output=True, text=True, timeout=20)
                        self.assertEqual(health.returncode, 0, health.stderr)
                        self.assertEqual(health.stdout, "")
                        state = subprocess.run([str(entrypoints / "statectl.sh"), "state"], env=env,
                                               capture_output=True, text=True, timeout=20)
                        self.assertEqual(state.returncode, 0, state.stderr)
                        self.assertEqual(json.loads(state.stdout)["state"], "standby")
                        for _ in range(2):
                            operational.client("activate")
                            wait_state("active")
                            operational.client("standby")
                            wait_state("standby")
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
                        self.assertEqual(len(starts.read_text().splitlines()), 2)

                        # Simulate power-up from a different persisted mode.
                        with sensor.lock:
                            firmware.active["operating_mode"] = "NORMAL"
                        wait_state("standby")
                        self.assertEqual(len(starts.read_text().splitlines()), 2)
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


if __name__ == "__main__":
    unittest.main()
