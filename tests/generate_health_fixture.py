"""Print a real Lyrical CDR fixture for the dashboard's decoder contract test.

Run in the Ouster runtime image with the ROS environment sourced.
"""
import base64
import json
from pathlib import Path
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker/entrypoints"))
from health import HealthReporter
from health_ros import ros_values
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from sensor_msgs.msg import Temperature
from rclpy.serialization import serialize_message


class Sensor:
    def telemetry(self):
        return {"internal_temperature_deg_c": 45.5, "input_voltage_mv": 23606, "input_current_ma": 758}

    def alerts(self):
        return {"active": [{"id": "POWER_LOW", "level": "ERROR", "msg": "input low"}], "log": [], "next_cursor": 1}


reporter = HealthReporter("lidar", "veh1", clock=lambda: 0)
reporter.collect(Sensor(), threading.Event())
message = DiagnosticArray()
for report in reporter.snapshot({"state": "standby", "at": 0}):
    message.status.append(DiagnosticStatus(level=(DiagnosticStatus.OK, DiagnosticStatus.WARN, DiagnosticStatus.ERROR, DiagnosticStatus.STALE)[report["level"]], name=report["name"], message=report["message"], hardware_id=report["hardware_id"],
        values=[KeyValue(key=k, value=v) for k, v in ros_values(report["values"]).items()]))
temperature = Temperature(temperature=45.5, variance=0.0)
print(json.dumps({"diagnostics": base64.b64encode(serialize_message(message)).decode(),
                  "temperature": base64.b64encode(serialize_message(temperature)).decode()}, indent=2))
