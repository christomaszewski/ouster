"""Exercise the installed generated interfaces with the deployment's Zenoh RMW."""

import importlib.util
import os
import subprocess
import sys
import unittest


@unittest.skipUnless(
    importlib.util.find_spec("rclpy") and importlib.util.find_spec("ouster_sensor_msgs"),
    "requires the packaged ROS/Ouster overlay",
)
class TypeSupportTests(unittest.TestCase):
    def test_ouster_interfaces_under_zenoh(self):
        env = dict(os.environ,
                   RMW_IMPLEMENTATION="rmw_zenoh_cpp",
                   ZENOH_ROUTER_CHECK_ATTEMPTS="-1",
                   ZENOH_CONFIG_OVERRIDE='connect/endpoints=[];'
                   'listen/endpoints=["tcp/127.0.0.1:0"];transport/shared_memory/enabled=false')
        for name in ("ZENOH_SESSION_CONFIG_URI", "ZENOH_SHM_ALLOC_SIZE"):
            env.pop(name, None)
        result = subprocess.run([sys.executable, "-c", """
import rclpy
from rclpy.serialization import serialize_message, deserialize_message
from rclpy.utilities import get_rmw_implementation_identifier
from ouster_sensor_msgs.msg import PacketMsg, Telemetry
from ouster_sensor_msgs.srv import GetConfig, SetConfig, GetMetadata

rclpy.init()
assert get_rmw_implementation_identifier() == 'rmw_zenoh_cpp'
node = rclpy.create_node('ouster_typesupport_smoke')
try:
    for kind in (PacketMsg, Telemetry):
        publisher = node.create_publisher(kind, 'smoke/' + kind.__name__, 1)
        message = kind()
        deserialize_message(serialize_message(message), kind)
        publisher.publish(message)
    for kind in (GetConfig, SetConfig, GetMetadata):
        node.create_service(kind, 'smoke/' + kind.__name__, lambda request, response: response)
finally:
    node.destroy_node()
    rclpy.shutdown()
"""], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
