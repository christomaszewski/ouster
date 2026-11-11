"""The telemetry stream check against a real ROS graph: sampled per heartbeat, never per packet.

Needs rclpy and the built ouster_sensor_msgs, so it runs in the packaged runtime image (CI sets
OUSTER_TEST_ENTRYPOINT_DIR to the shipped entrypoints) and is skipped on a bare host. There it
runs twice: under the RMW the environment selects (Fast DDS in the image), and under
rmw_zenoh_cpp through a router of its own, which is what deployments run."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = Path(os.environ.get("OUSTER_TEST_ENTRYPOINT_DIR", ROOT / "docker/entrypoints"))
HAS_ROS = all(importlib.util.find_spec(module) is not None
              for module in ("rclpy", "ouster_sensor_msgs", "diagnostic_msgs"))


def zenoh_router():
    """Path of rmw_zenoh_cpp's router executable, or None where that RMW is not installed."""
    try:
        from ament_index_python.packages import get_package_prefix
        router = Path(get_package_prefix("rmw_zenoh_cpp")) / "lib/rmw_zenoh_cpp/rmw_zenohd"
    except Exception:
        return None
    return router if router.is_file() else None

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
    sensor, driver, sensor_seen = Sensor(), Driver(), ({}, 0)
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
        from rclpy.utilities import get_rmw_implementation_identifier
        from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
        from ouster_sensor_msgs.msg import Telemetry

        # A requested RMW that did not load would run this test under another one unnoticed.
        self.assertEqual(get_rmw_implementation_identifier(),
                         os.environ.get("RMW_IMPLEMENTATION") or get_rmw_implementation_identifier())
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
            # One observation per heartbeat that found a new packet: never one per packet. The
            # lower bound only has to tell sampling from not observing at all, so it leaves room
            # for a loaded machine that delays a heartbeat past its packets.
            self.assertGreaterEqual(observed, heartbeats // 2)
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

    @unittest.skipUnless(zenoh_router(), "needs rmw_zenoh_cpp and its router (the packaged runtime image)")
    def test_same_checks_under_rmw_zenoh_through_a_private_router(self):
        """The same checks under the RMW deployments run.

        rclpy cannot change RMW inside a process, so the test above runs again in a child whose
        environment selects rmw_zenoh_cpp; the monitor it starts inherits that. Both sessions
        connect to a router started here on a loopback port of its own: IPv4, because CI's
        `--network none` container has no IPv6 for the router's default tcp/[::]:7447 listener,
        and never the default port, so the test cannot join a router that is in use. Without
        the router the two sessions do not find each other (rmw_zenoh disables multicast
        scouting), so a pass means the traffic went through it."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        endpoint = f"tcp/127.0.0.1:{port}"
        env = {name: value for name, value in os.environ.items()
               if name not in ("ZENOH_SESSION_CONFIG_URI", "ZENOH_ROUTER_CONFIG_URI",
                               "ZENOH_ROUTER_CHECK_ATTEMPTS", "ZENOH_SHM_ALLOC_SIZE")}
        env["RMW_IMPLEMENTATION"] = "rmw_zenoh_cpp"
        router = subprocess.Popen(
            [str(zenoh_router())], env=dict(env, ZENOH_CONFIG_OVERRIDE=f'listen/endpoints=["{endpoint}"]'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def stop_router():
            if router.poll() is None:
                router.terminate()
                try:
                    router.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    router.kill()
            return router.communicate()[0][-2000:]

        try:
            deadline = time.monotonic() + 30
            while True:
                self.assertIsNone(router.poll(), "the zenoh router exited")
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    self.assertLess(time.monotonic(), deadline, f"no zenoh router on {endpoint}")
                    time.sleep(0.2)
            name = f"{type(self).__module__}.{type(self).__qualname__}." \
                   "test_stream_is_sampled_per_heartbeat_and_its_loss_is_reported"
            child = subprocess.run(
                [sys.executable, "-m", "unittest", "-v", name], cwd=Path(__file__).resolve().parent,
                env=dict(env, ZENOH_CONFIG_OVERRIDE=f'connect/endpoints=["{endpoint}"];'
                                                    'transport/shared_memory/enabled=false'),
                capture_output=True, text=True, timeout=240)
            output = child.stdout + child.stderr
        except BaseException:
            stop_router()
            raise
        router_log = stop_router()
        self.assertEqual(child.returncode, 0, f"{output[-4000:]}\nrouter: {router_log}")
        self.assertRegex(output, r"test_stream_is_sampled_per_heartbeat_and_its_loss_is_reported .*ok\b",
                         "the child did not run the test")


if __name__ == "__main__":
    unittest.main()
