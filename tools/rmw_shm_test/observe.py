"""Check synthetic PointCloud2 payloads using the installed, unmodified subscriber."""
import hashlib
import json
from pathlib import Path
import sys
import time

import rclpy
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import PointCloud2

rclpy.init()
node = rclpy.create_node("shm_probe_sub")
expected = hashlib.sha256(bytes((i * 31 + 7) % 256 for i in range(128 * 2048 * 48))).hexdigest()
received, sizes = [], []


def got(raw):
    msg = deserialize_message(raw, PointCloud2)
    assert (msg.width, msg.height, msg.point_step, msg.row_step, msg.header.frame_id) == (2048, 128, 48, 98304, "probe")
    assert msg.header.stamp.nanosec == 0 and not msg.is_bigendian and not msg.is_dense
    assert hashlib.sha256(msg.data).hexdigest() == expected
    assert [(f.name, f.offset, f.datatype, f.count) for f in msg.fields] == [
        ("x", 0, 7, 1), ("y", 4, 7, 1), ("z", 8, 7, 1)
    ]
    assert len(raw) == 12583025, f"Unexpected raw serialized length: {len(raw)}"
    received.append(msg.header.stamp.sec)
    sizes.append(len(raw))


subscription = node.create_subscription(PointCloud2, "/shm_probe", got, 10, raw=True)
start = time.monotonic()
try:
    while len(received) < 40 and time.monotonic() - start < 20:
        rclpy.spin_once(node, timeout_sec=0.2)
finally:
    result = {"frames": len(received), "sequence": received, "payload_sha256": expected,
              "serialized_sizes": sorted(set(sizes))}
    Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + "\n")
    node.destroy_node()
    rclpy.shutdown()
assert received == list(range(1, 41)), result
