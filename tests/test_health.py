"""Read-only telemetry, alert reconciliation, independent expiry and recovery."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_operational_state import FakeClock, FakeDriver, Firmware, SensorError, SensorHTTP, operational
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

    def test_telemetry_is_one_bounded_read_only_request(self):
        firmware = Firmware("v2.4.0")
        firmware.telemetry.update(input_current_ma=758, input_voltage_mv=23606,
                                  phase_lock_status="DISABLED", timestamp_ns=2962666299310)
        with patch("urllib.request.urlopen", side_effect=firmware) as request:
            values = telemetry_values(SensorHTTP("sensor").telemetry())
        self.assertEqual(values["temp.internal_c"], 45)  # degrees Celsius, as the sensor reports it
        self.assertEqual(firmware.calls, [("GET", "/api/v1/sensor/telemetry", "")])
        self.assertEqual(request.call_args.kwargs["timeout"], 2)
        self.assertEqual(firmware.active, firmware.persisted)
        with patch.object(SensorHTTP, "request", return_value="[]"), self.assertRaises(SensorError):
            SensorHTTP("sensor").telemetry()

    def test_invalid_temperature_is_unavailable_and_zero_or_negative_is_a_reading(self):
        for payload in ({}, *({"internal_temperature_deg_c": value}
                              for value in (None, True, "45", float("nan"), float("inf")))):
            with self.subTest(payload=payload):
                values = telemetry_values(payload)
                self.assertIsNone(values["temp.internal_c"])
                self.assertEqual(values["health.metric.temp.internal_c.state"], "unavailable")
        for value in (0, -12.5, 48.25):
            with self.subTest(value=value):
                values = telemetry_values({"internal_temperature_deg_c": value})
                self.assertEqual(values["temp.internal_c"], value)
                self.assertNotIn("health.metric.temp.internal_c.state", values)

    def test_only_a_fresh_reading_is_handed_on_for_the_temperature_topic(self):
        self.assertEqual(self.reporter.collect(self.sensor, self.changing), 42)
        self.sensor.telemetry.side_effect = OSError("disconnected")
        self.assertIsNone(self.reporter.collect(self.sensor, self.changing), "a failed read replays nothing")
        self.sensor.telemetry.side_effect = None
        self.sensor.telemetry.return_value = {"input_voltage_mv": 24000, "input_current_ma": 700}
        self.assertIsNone(self.reporter.collect(self.sensor, self.changing), "no reading is not zero")

        def read_during_transition():
            self.changing.set()
            return {"internal_temperature_deg_c": 50}

        self.sensor.telemetry.side_effect = read_during_transition
        polls = self.sensor.alerts.call_count
        self.assertIsNone(self.reporter.collect(self.sensor, self.changing))
        self.assertEqual(self.sensor.alerts.call_count, polls, "a transition that began mid-read ends the poll")
        self.assertIsNone(self.rows()["temperature"]["values"]["temp.internal_c"],
                          "the overtaken read must not replace the last sample")

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

    def identified(self, initialization_id, resets=0):
        self.reporter.identify({"prod_sn": "992201000123", "initialization_id": initialization_id}, resets)

    def alerts(self, next_cursor, first=0):
        log = [{"id": f"0x{n:x}", "level": "NOTICE", "cursor": n} for n in range(first, next_cursor)]
        self.reporter.update_alerts({"active": [], "log": log, "next_cursor": next_cursor})
        return self.rows()["sensor alerts"]["values"]

    def test_supervisors_own_reinitialization_is_not_a_history_gap(self):
        self.identified(1)
        epoch = self.alerts(3)["alerts.epoch"]
        self.identified(2)  # a mode change by the supervisor: the count of resets did not move
        after = self.alerts(4)
        self.assertFalse(after["alerts.history_gap"])
        self.assertEqual(after["alerts.epoch"], epoch, "cursors that carried on keep their scope")
        self.identified(3)
        restarted = self.alerts(1)  # a log that counts from zero after the reinitialization
        self.assertFalse(restarted["alerts.history_gap"])
        self.assertNotEqual(restarted["alerts.epoch"], epoch, "reused cursors need a new scope")

    def test_events_missed_across_an_own_reinitialization_are_still_a_gap(self):
        self.identified(1)
        self.alerts(3)
        self.identified(2)
        self.assertTrue(self.alerts(9, first=5)["alerts.history_gap"])
        self.reporter = HealthReporter("lidar", "vehicle", clock=self.clock)
        self.identified(1)
        self.alerts(3)
        self.identified(2)
        self.assertTrue(self.alerts(2, first=1)["alerts.history_gap"], "a restarted log must begin at zero")

    def test_identity_change_nobody_here_caused_breaks_history(self):
        self.identified(7, resets=3)  # resets from before this reporter started are not its gaps
        first = self.alerts(3)
        self.assertFalse(first["alerts.history_gap"])
        self.identified(8, resets=4)
        after = self.alerts(3)
        self.assertTrue(after["alerts.history_gap"])
        self.assertNotEqual(after["alerts.epoch"], first["alerts.epoch"])
        self.identified(9, resets=4)
        self.assertTrue(self.alerts(4)["alerts.history_gap"], "the flag lasts for the reporter's lifetime")
        self.reporter = HealthReporter("lidar", "vehicle", clock=self.clock)
        self.reporter.identify({"prod_sn": "992201000123", "initialization_id": 1})
        self.alerts(3)
        self.reporter.identify({"prod_sn": "992201000123", "initialization_id": 2})  # no supervisor to ask
        self.assertTrue(self.alerts(3)["alerts.history_gap"])

    def test_history_gap_through_the_supervisor_and_a_modelled_sensor(self):
        """The supervisor's mode changes leave alerts.history_gap false; a reboot sets it."""
        firmware = Firmware("v3.1.0")
        firmware.alerts = {"active": [], "log": [{"id": "0x1", "level": "NOTICE", "cursor": 0}], "next_cursor": 1}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch("urllib.request.urlopen", side_effect=firmware)
        patcher.start()
        self.addCleanup(patcher.stop)
        sensor = SensorHTTP("sensor", clock=self.clock, sleep=self.clock.sleep)
        controller = operational.Controller(sensor, FakeDriver([]), "standby", Path(temp.name) / "target",
                                            clock=self.clock)

        def summary():  # what health_ros.collect does on each poll
            info, resets = controller.sensor_seen
            self.reporter.identify(info, resets)
            self.reporter.collect(sensor, controller.changing)
            return self.rows()["sensor alerts"]["values"]

        controller.transition()
        epoch = summary()["alerts.epoch"]
        for target in ("active", "standby", "active", "standby"):
            controller.transition(target)
            after = summary()
            self.assertFalse(after["alerts.history_gap"], target)
            self.assertEqual(after["alerts.epoch"], epoch)
        self.assertEqual(firmware.initialization_id, 7109744 + 4)
        firmware.reboot()
        controller.transition()
        after = summary()
        self.assertTrue(after["alerts.history_gap"])
        self.assertNotEqual(after["alerts.epoch"], epoch)

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
