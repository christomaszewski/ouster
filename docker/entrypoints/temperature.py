"""Read-only HTTP temperature publisher, independent of the lidar driver."""

from contextlib import contextmanager
import logging
import threading
import time

from sensor_http import SensorError

LOG = logging.getLogger("ouster-temperature")


def poll_temperature(sensor, publish, stop_event, changing, *, interval=5,
                     clock=time.monotonic):
    """Skip failed samples and transitions; never replay a cached reading."""
    next_warning = 0
    while not stop_event.is_set():
        if not changing.is_set():
            try:
                value = sensor.temperature()
            except (SensorError, OSError, ValueError) as error:
                if clock() >= next_warning:
                    LOG.warning("temperature sample skipped: %s", error)
                    next_warning = clock() + 60
            else:
                if not stop_event.is_set() and not changing.is_set():
                    publish(value)
        stop_event.wait(interval)


@contextmanager
def temperature_monitor(sensor, namespace, frame, stop_event, changing):
    # Lazy imports keep the state client and HTTP tests independent of ROS.
    import rclpy
    from rclpy.clock import Clock, ClockType
    from rclpy.context import Context
    from rclpy.signals import SignalHandlerOptions
    from sensor_msgs.msg import Temperature

    context = Context()
    node = None
    thread = None
    try:
        # The supervisor owns SIGINT/SIGTERM. This publisher has no callbacks or
        # services, so it needs no executor and cannot hold up mode transitions.
        rclpy.init(args=[], context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = rclpy.create_node("sensor_temperature", namespace=namespace, context=context,
                                use_global_arguments=False, start_parameter_services=False,
                                enable_rosout=False)
        # Reliable, volatile, depth 1: compatible with reliable and best-effort
        # subscribers without presenting old samples to newly joined readers.
        publisher = node.create_publisher(Temperature, "temperature", 1)
        receipt_clock = Clock(clock_type=ClockType.SYSTEM_TIME)

        def publish(value):
            message = Temperature()
            message.header.stamp = receipt_clock.now().to_msg()
            message.header.frame_id = frame
            message.temperature = value
            message.variance = 0.0  # unknown, per sensor_msgs/Temperature
            publisher.publish(message)

        def run():
            try:
                poll_temperature(sensor, publish, stop_event, changing)
            except Exception:
                LOG.exception("temperature publisher failed; stopping supervisor")
                stop_event.set()

        thread = threading.Thread(target=run, name="sensor-temperature", daemon=True)
        thread.start()
        LOG.info("publishing %s in Celsius every 5 seconds when available", publisher.topic_name)
        yield
    finally:
        stop_event.set()
        if thread is not None:
            thread.join()  # at most the in-flight HTTP request; wait is interruptible
        if node is not None:
            node.destroy_node()
        context.try_shutdown()
