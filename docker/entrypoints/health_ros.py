"""ROS adapter: slow HTTP collection never blocks diagnostics heartbeats."""
from contextlib import contextmanager
import json
import logging
import os
import threading
import time

from health import HealthReporter
from lifecycle_ros import LifecycleClient

LOG = logging.getLogger("ouster-health")


def ros_values(values):
    """Diagnostic KeyValue is string-only; null has a companion metric state."""
    return {key: value if isinstance(value, str) else json.dumps(value, allow_nan=False)
            for key, value in values.items()}


@contextmanager
def health_monitor(controller, namespace, params, stop_event, changing):
    import rclpy
    from rclpy.callback_groups import CallbackGroup
    from rclpy.clock import Clock, ClockType
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    from sensor_msgs.msg import Temperature
    from ouster_sensor_msgs.msg import Telemetry

    class Unspun(CallbackGroup):
        """Entities the executor never waits on; their owner takes from them directly."""

        def can_execute(self, entity):
            return False

        def beginning_execution(self, entity):
            return False

        def ending_execution(self, entity):
            pass

    reporter = HealthReporter(os.environ.get("OUSTER_NAME") or namespace.strip("/"),
        os.environ.get("VEHICLE_ID", ""), json.loads(os.environ.get("OUSTER_HEALTH_CONFIG") or "{}"))
    context, node, worker, spinner, executor = Context(), None, None, None, None
    spin_stop = threading.Event()
    try:
        rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = rclpy.create_node("sensor_health", namespace=namespace, context=context,
            use_global_arguments=False, start_parameter_services=False, enable_rosout=False)
        diagnostics = node.create_publisher(DiagnosticArray, "/diagnostics", 10)
        temperature = node.create_publisher(Temperature, "temperature", 1)
        receipt_clock = Clock(clock_type=ClockType.SYSTEM_TIME)
        stream_enabled = "TLM" in str(params.get("proc_mask", "IMG|PCL|IMU|SCAN|TLM")).split("|")
        telemetry = None
        if stream_enabled:
            # The driver publishes one Telemetry message per lidar packet (640-1280 per second).
            # A Python callback for each cost 20-30% of a CPU core only to learn that packets
            # arrive. The stream check needs "did one arrive since the last heartbeat": the
            # middleware keeps the newest (depth 1), the executor never waits on it, and each
            # heartbeat takes it. Nothing is deserialized; best effort matches either driver QoS.
            telemetry = node.create_subscription(
                Telemetry, "telemetry", lambda _: None,
                QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
                callback_group=Unspun(), raw=True)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        controller.driver.lifecycle_client = LifecycleClient(node, controller.driver.node)
        next_warning = 0

        def guarded(action):
            nonlocal next_warning
            try:
                action()
            except Exception:
                if time.monotonic() >= next_warning:
                    LOG.exception("health reporting failed; sensor supervisor continues")
                    next_warning = time.monotonic() + 60

        def sample_stream():
            if telemetry is None:
                return
            with telemetry.handle:
                if telemetry.handle.take_message(telemetry.msg_type, telemetry.raw) is not None:
                    reporter.packet_seen()

        def publish_health():
            if stop_event.is_set():
                return
            message = DiagnosticArray()
            message.header.stamp = receipt_clock.now().to_msg()
            for report in reporter.snapshot(controller.health_state(), changing=changing.is_set(), stream_enabled=stream_enabled):
                row = DiagnosticStatus()
                row.level = (DiagnosticStatus.OK, DiagnosticStatus.WARN, DiagnosticStatus.ERROR, DiagnosticStatus.STALE)[report["level"]]
                row.name, row.message, row.hardware_id = report["name"], report["message"], report["hardware_id"]
                row.values = [KeyValue(key=k, value=v) for k, v in ros_values(report["values"]).items()]
                message.status.append(row)
            diagnostics.publish(message)

        def heartbeat():
            guarded(sample_stream)  # a sampling failure must not silence the diagnostics
            guarded(publish_health)

        node.create_timer(reporter.settings["interval_s"], heartbeat)

        def collect():
            info, resets = controller.sensor_seen
            if info:
                reporter.identify(info, resets)
            value = reporter.collect(controller.sensor, changing)
            if value is not None and not changing.is_set() and not stop_event.is_set():
                message = Temperature()
                message.header.stamp = receipt_clock.now().to_msg()
                message.header.frame_id = params.get("sensor_frame", "os_sensor")
                message.temperature = float(value)
                message.variance = 0.0
                temperature.publish(message)

        def collect_loop():
            while not stop_event.is_set():
                guarded(collect)
                stop_event.wait(reporter.settings["poll_interval_s"])

        def spin_loop():
            # SIGTERM stops health polling, but lifecycle replies still need
            # this executor while the supervisor drains/stops the driver.
            while not spin_stop.is_set():
                guarded(lambda: executor.spin_once(timeout_sec=0.2))

        worker = threading.Thread(target=collect_loop, name="sensor-health-http", daemon=True)
        spinner = threading.Thread(target=spin_loop, name="sensor-health-ros", daemon=True)
        worker.start()
        spinner.start()
        yield
    finally:
        stop_event.set()
        controller.driver.lifecycle_client = None
        spin_stop.set()
        if executor:
            executor.wake()
        for thread in (worker, spinner):
            if thread:
                thread.join()
        if executor:
            executor.shutdown()
        if node:
            node.destroy_node()
        context.try_shutdown()
