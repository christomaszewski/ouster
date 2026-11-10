"""The telemetry stream check against a real ROS graph: sampled per heartbeat, never per packet.

Needs rclpy and the built ouster_sensor_msgs, so it runs in the packaged runtime image (CI sets
OUSTER_TEST_ENTRYPOINT_DIR to the shipped entrypoints) and is skipped on a bare host."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = Path(os.environ.get("OUSTER_TEST_ENTRYPOINT_DIR", ROOT / "docker/entrypoints"))
HAS_ROS = all(importlib.util.find_spec(module) is not None
              for module in ("rclpy", "ouster_sensor_msgs", "diagnostic_msgs"))

# The shipped monitor with a stand-in controller: an active instance whose sensor answers.
MONITOR = r"""
import signal, sys, threading, time
sys.path.insert(0, sys.argv[1])
import health_ros

class Sensor:
    def telemetry(self):
        return {"internal_temperature_deg_c": 45, "input_voltage_mv": 24000, "input_current_ma": 700}
    def alerts(self):
        return {"active": [], "log": [], "next_cursor": 0}

class Driver:
    node, lifecycle_client = "/stream_test/os_driver", None

class Controller:
    sensor, driver, sensor_info = Sensor(), Driver(), {}
    def health_state(self):
        return {"state": "active", "detail": "test", "at": time.monotonic(), "target": "active", "error": ""}

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
with health_ros.health_monitor(Controller(), "/stream_test", {}, stop, threading.Event()):
    stop.wait(120)
"""


@unittest.skipUnless(HAS_ROS, "needs rclpy and ouster_sensor_msgs (the packaged runtime image)")
class StreamSamplingTests(unittest.TestCase):
    HEARTBEAT = 0.2
    RATE = 400  # packets per second, the order of the driver's per-packet telemetry

    def test_stream_is_sampled_per_heartbeat_and_its_loss_is_reported(self):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.signals import SignalHandlerOptions
        from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
        from ouster_sensor_msgs.msg import Telemetry

        env = dict(os.environ, ROS_DOMAIN_ID="87", OUSTER_NAME="stream_test",
                   OUSTER_HEALTH_CONFIG=json.dumps({"interval_s": self.HEARTBEAT}))
        previous = os.environ.get("ROS_DOMAIN_ID")
        os.environ["ROS_DOMAIN_ID"] = "87"
        self.addCleanup(lambda: os.environ.pop("ROS_DOMAIN_ID") if previous is None
                        else os.environ.__setitem__("ROS_DOMAIN_ID", previous))
        monitor = subprocess.Popen([sys.executable, "-c", MONITOR, str(ENTRYPOINTS)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        context = Context()
        rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = rclpy.create_node("stream_test_driver", namespace="/stream_test", context=context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        rows = []

        def stream_rows(message):
            for status in message.status:
                if status.name == "stream_test: stream":
                    rows.append((status.level, status.message,
                                 int(dict((v.key, v.value) for v in status.values)["packets.received"])))

        node.create_subscription(DiagnosticArray, "/diagnostics", stream_rows, 10)
        publisher = node.create_publisher(Telemetry, "telemetry", qos_profile_sensor_data)
        publishing = threading.Event()
        sent = [0]

        def publish():
            message, period, following = Telemetry(), 1.0 / self.RATE, time.monotonic()
            while not finished.is_set():
                if publishing.is_set():
                    publisher.publish(message)
                    sent[0] += 1
                following += period
                time.sleep(max(0.0, following - time.monotonic()))

        finished = threading.Event()
        driver = threading.Thread(target=publish, daemon=True)

        def spin_until(condition, timeout, what):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.assertIsNone(monitor.poll(), "health monitor exited")
                executor.spin_once(timeout_sec=0.05)
                if condition():
                    return
            monitor.terminate()
            self.fail(f"{what}; last stream rows: {rows[-3:]}; monitor: {monitor.communicate(timeout=10)[0][-2000:]}")

        try:
            driver.start()
            publishing.set()
            spin_until(lambda: rows and rows[-1][1] == "receiving", 20, "stream never reported receiving")
            first, start_sent, start_rows = rows[-1][2], sent[0], len(rows)
            spin_until(lambda: len(rows) >= start_rows + 10, 20, "diagnostics stopped")
            heartbeats, packets = len(rows) - start_rows, sent[0] - start_sent
            observed = rows[-1][2] - first
            self.assertGreater(packets, 5 * heartbeats, "the fixture must outpace the heartbeat")
            self.assertEqual(rows[-1][0], DiagnosticStatus.OK)
            # One observation per heartbeat that found a new packet: never one per packet.
            self.assertGreaterEqual(observed, heartbeats - 2)
            self.assertLessEqual(observed, heartbeats + 1)

            publishing.clear()
            spin_until(lambda: rows[-1][0] == DiagnosticStatus.ERROR, 15, "a silent stream was not reported")
            self.assertEqual(rows[-1][1], "no lidar telemetry packets")
            stopped_at = rows[-1][2]
            publishing.set()
            spin_until(lambda: rows[-1][0] == DiagnosticStatus.OK and rows[-1][2] > stopped_at, 15,
                       "the stream did not recover")
        finally:
            finished.set()
            if driver.is_alive():
                driver.join(timeout=5)
            if monitor.poll() is None:
                monitor.terminate()
                try:
                    monitor.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    monitor.kill()
                    monitor.wait()
            if monitor.stdout:
                monitor.stdout.close()
            executor.shutdown()
            node.destroy_node()
            context.try_shutdown()


if __name__ == "__main__":
    unittest.main()
