"""Read-only telemetry, alert reconciliation, independent expiry and recovery."""
import json
import threading
import unittest
from unittest.mock import Mock

from test_operational_state import FakeClock
from health import HealthReporter, telemetry_values, config, ERROR, STALE
from health_ros import ros_values


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.reporter = HealthReporter("lidar", "vehicle", clock=self.clock)
        self.sensor = Mock()
        self.sensor.telemetry.return_value = {"internal_temperature_deg_c": 42, "input_voltage_mv": 23606, "input_current_ma": 758}
        self.sensor.alerts.return_value = {"active": [], "log": [], "next_cursor": 0}
        self.changing = threading.Event()

    def rows(self, mode="standby"):
        return {s["name"].split(": ", 1)[1]: s for s in self.reporter.snapshot({"state": mode, "at": self.clock()})}

    def test_same_poll_units_and_missing_temperature_do_not_hide_power(self):
        values = telemetry_values(self.sensor.telemetry())
        self.assertEqual(values["supply.power_w"], 17.893348)
        self.assertEqual(values["supply.voltage_v"], 23.606)
        self.assertEqual(values["supply.current_a"], 0.758)
        values = telemetry_values({"input_voltage_mv": 24000, "input_current_ma": 0}, temperature_supported=False)
        self.assertEqual(values["supply.power_w"], 0)
        self.assertIsNone(values["temp.internal_c"])
        self.assertEqual(values["health.metric.temp.internal_c.state"], "unsupported")
        for bad in (True, "758", float("nan"), float("inf"), None):
            values = telemetry_values({"input_voltage_mv": 24000, "input_current_ma": bad})
            self.assertIsNone(values["supply.power_w"])

    def test_failures_retain_age_sample_identity_and_last_error_then_recover(self):
        self.reporter.settings["limits"] = {"temp.internal_c": {"error_above": 40}}
        self.reporter.collect(self.sensor, self.changing)
        first = self.rows()["temperature"]
        self.assertEqual(first["level"], ERROR)
        self.clock.now = 4
        self.sensor.telemetry.side_effect = RuntimeError("timeout")
        self.reporter.collect(self.sensor, self.changing)
        held = self.rows()["temperature"]
        self.assertEqual(held["values"]["health.sample_id"], first["values"]["health.sample_id"])
        self.assertEqual(held["values"]["health.sample_age_s"], 4)
        self.assertEqual(held["values"]["health.availability"], "retrying")
        self.clock.now = 20
        stale = self.rows()["temperature"]
        self.assertEqual(stale["level"], STALE)
        self.assertEqual(stale["values"]["health.last_level"], ERROR)
        self.sensor.telemetry.side_effect = None
        self.reporter.collect(self.sensor, self.changing)
        self.assertEqual(self.rows()["temperature"]["values"]["health.sample_age_s"], 0)
        self.assertNotEqual(self.rows()["temperature"]["values"]["health.sample_id"], first["values"]["health.sample_id"])

    def test_alerts_clear_only_on_valid_complete_response_and_keep_vendor_text(self):
        fault = {"id": "0x100", "level": "ERROR", "category": "POWER", "msg": "input low", "msg_verbose": "Check supply", "active": True, "cursor": 0}
        self.sensor.alerts.return_value = {"active": [fault], "log": [fault], "next_cursor": 1}
        self.reporter.collect(self.sensor, self.changing)
        self.assertEqual(self.rows()["alert/0x100"]["level"], ERROR)
        self.sensor.alerts.return_value = {"active": [{}]}
        self.reporter.collect(self.sensor, self.changing)
        self.assertTrue(self.rows()["alert/0x100"]["values"]["alert.active"])
        self.assertEqual(self.rows()["power"]["level"], 0)
        self.sensor.alerts.return_value = {"active": [], "log": [{**fault, "active": False, "cursor": 1}], "next_cursor": 2}
        self.reporter.collect(self.sensor, self.changing)
        cleared = self.rows()["alert/0x100"]
        self.assertEqual(cleared["level"], 0)
        self.assertFalse(cleared["values"]["alert.active"])
        self.assertEqual(cleared["values"]["alert.detail"], "Check supply")
        self.assertIn('"active":false', self.rows()["sensor alerts"]["values"]["alerts.log"])

    def test_cursor_reset_and_gaps_are_visible(self):
        self.reporter.update_alerts({"active": [], "next_cursor": 8})
        epoch = self.rows()["sensor alerts"]["values"]["alerts.epoch"]
        self.reporter.update_alerts({"active": [], "next_cursor": 1})
        self.assertTrue(self.rows()["sensor alerts"]["values"]["alerts.history_gap"])
        self.assertNotEqual(self.rows()["sensor alerts"]["values"]["alerts.epoch"], epoch)

    def test_standby_transition_and_activation_stream_grace(self):
        self.clock.now = 100
        self.assertEqual(self.rows()["stream"]["level"], 0)
        self.assertEqual(self.rows("active")["stream"]["level"], 0)
        self.clock.now = 106
        self.assertEqual(self.rows("active")["stream"]["level"], ERROR)
        self.reporter.packet_seen()
        self.assertEqual(self.rows("active")["stream"]["level"], 0)
        self.changing.set()
        self.reporter.collect(self.sensor, self.changing)
        self.sensor.telemetry.assert_not_called()

    def test_ros_strings_preserve_null_and_boolean_and_config_rejects_invalid_periods(self):
        self.assertEqual(ros_values({"missing": None, "active": False, "zero": 0}), {"missing": "null", "active": "false", "zero": "0"})
        for raw in ({"interval_s": 0}, {"poll_interval_s": True}, {"limits": {"a": {"err_above": 5}}}):
            with self.assertRaises(ValueError):
                config(raw)
        json.dumps(self.rows(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
