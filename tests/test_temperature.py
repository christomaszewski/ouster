"""Temperature units, unsupported hardware, and recovery without stale samples."""

import json
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

from test_operational_state import Firmware, SensorError, SensorHTTP
from temperature import poll_temperature


class TemperatureTests(unittest.TestCase):
    def test_fw24_temperature_is_celsius_and_read_only(self):
        firmware = Firmware("v2.4.0")
        firmware.telemetry.update(input_current_ma=758, input_voltage_mv=23606,
                                  phase_lock_status="DISABLED", timestamp_ns=2962666299310)
        with patch("urllib.request.urlopen", side_effect=firmware) as request:
            self.assertEqual(SensorHTTP("sensor").temperature(), 45.0)
        self.assertEqual(firmware.calls, [("GET", "/api/v1/sensor/telemetry", "")])
        self.assertEqual(request.call_args.kwargs["timeout"], 2)
        self.assertEqual(firmware.active, firmware.persisted)

    def test_unavailable_or_invalid_temperature_is_not_zero(self):
        sensor = SensorHTTP("sensor")
        payloads = [{}, {"internal_temperature_deg_c": None}, [],
                    *({"internal_temperature_deg_c": value}
                      for value in (True, "45", float("nan"), float("inf")))]
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(sensor, "request", return_value=json.dumps(payload)):
                with self.assertRaises(SensorError):
                    sensor.temperature()

    def test_zero_negative_and_fractional_temperatures_are_valid(self):
        sensor = SensorHTTP("sensor")
        for value in (0, -12.5, 48.25):
            with self.subTest(value=value), patch.object(sensor, "request", return_value=json.dumps(
                    {"internal_temperature_deg_c": value})):
                self.assertEqual(sensor.temperature(), float(value))

    def test_failed_reads_skip_samples_and_resume_without_replaying(self):
        stop = threading.Event()
        sensor = Mock()
        sensor.temperature.side_effect = [45.0, urllib.error.URLError("disconnected"),
                                          SensorError("unavailable"), ValueError("bad JSON"), 46.0]
        readings = []

        def publish(value):
            readings.append(value)
            if len(readings) == 2:
                stop.set()

        with self.assertLogs("ouster-temperature", level="WARNING") as logs:
            poll_temperature(sensor, publish, stop, threading.Event(), interval=0)
        self.assertEqual(readings, [45.0, 46.0])
        self.assertEqual(sensor.temperature.call_count, 5)
        self.assertEqual(len(logs.output), 1, "outages must not flood logs")

    def test_transition_started_during_http_request_drops_sample(self):
        stop, changing = threading.Event(), threading.Event()
        sensor, publish = Mock(), Mock()

        def read():
            changing.set()
            return 45.0

        sensor.temperature.side_effect = read
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            poll_temperature(sensor, publish, stop, changing)
        publish.assert_not_called()

    def test_transition_and_shutdown_do_not_poll_sensor(self):
        stop, changing = threading.Event(), threading.Event()
        sensor, publish = Mock(), Mock()
        changing.set()
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            poll_temperature(sensor, publish, stop, changing)
        poll_temperature(sensor, publish, stop, changing)
        sensor.temperature.assert_not_called()
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
