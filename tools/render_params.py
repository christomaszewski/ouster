#!/usr/bin/env python3
# Copyright 2026 Chris Tomaszewski
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Render a generic *rig* sensor config into the upstream Ouster driver's ROS 2 params file.

The sensor config is the single source of truth (uniform across all rig services). `driver_params`
is OPAQUE -- copied verbatim into ros__parameters -- while the `connection` block is DERIVED onto
upstream's flat connection params (sensor_hostname / udp_dest / lidar_port / imu_port). Used by
`ouster-up`:

  render_params.py <config.yaml>          # -> the ROS 2 params YAML on stdout
  render_params.py --env <config.yaml>    # -> SERVICE/NAME/NAMESPACE/TYPE env lines

The params doc is keyed by the `/**` wildcard node so it binds regardless of the namespace rig
pushes via `ouster_ns`. (Upstream's own driver_params.yaml keys the explicit node path
`ouster/os_driver`, which would NOT match a custom namespace -- so do NOT copy that keying.)
Needs PyYAML on the host (apt: python3-yaml).
"""
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("render_params: PyYAML required on the host (apt install python3-yaml)\n")
    sys.exit(2)


# Instance identity tokens end up in compose project names, ROS namespaces, staging file
# paths, and an eval'd env block in the launcher — keep them to a safe, unambiguous alphabet.
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def require_ident(what: str, value: str) -> str:
    if not _IDENT_RE.match(value):
        sys.stderr.write(
            f"render_params: {what} {value!r} must match [a-z][a-z0-9_]* "
            "(it becomes a compose project / ROS namespace / file name)\n")
        sys.exit(2)
    return value


def _pick(block: dict, out: dict, key: str, src_key: str, cast) -> None:
    """Copy block[src_key] into out[key] (cast applied) only when the user provided it —
    absent keys are OMITTED so the driver's own schema defaults apply instead of this
    script fabricating divergent ones."""
    if src_key in block:
        out[key] = cast(block[src_key])


def derive_connection(connection: dict) -> dict:
    """Map the generic `connection` block onto upstream ouster driver_params keys.

    Ouster's connection params are FLAT top-level keys in ros__parameters (sensor_hostname,
    udp_dest, lidar_port, imu_port) -- unlike sbg's nested `transport:` schema -- so they merge
    into the same map as driver_params. Only the `lidar` type is supported; anything else is a
    config error surfaced here (NOT silently rendered into params the driver would reject).
    """
    ttype = str(connection.get("type") or "lidar")
    if ttype != "lidar":
        sys.stderr.write(f"render_params: unsupported connection.type {ttype!r} "
                         "(this driver supports: lidar)\n")
        sys.exit(2)
    block = connection.get("lidar") or {}
    out: dict = {}
    _pick(block, out, "sensor_hostname", "sensor_hostname", str)
    _pick(block, out, "udp_dest", "udp_dest", str)
    _pick(block, out, "lidar_port", "lidar_port", int)
    _pick(block, out, "imu_port", "imu_port", int)
    return out


def main() -> int:
    args = sys.argv[1:]
    env_mode = bool(args and args[0] == "--env")
    if env_mode:
        args = args[1:]
    if len(args) != 1:
        sys.stderr.write("usage: render_params.py [--env] <sensor-config.yaml>\n")
        return 2

    with open(args[0]) as handle:
        cfg = yaml.safe_load(handle) or {}

    service = require_ident("service", str(cfg.get("service") or "ouster"))
    name = require_ident("name", str(cfg.get("name") or "ouster"))
    connection = cfg.get("connection") or {}
    ttype = str(connection.get("type") or "lidar")
    namespace = require_ident("ros.namespace", str((cfg.get("ros") or {}).get("namespace") or name))

    if env_mode:
        lines = [f"SERVICE={service}", f"NAME={name}", f"NAMESPACE=/{namespace}", f"TYPE={ttype}"]
        print("\n".join(lines))
        return 0

    # params mode: connection-derived keys first, then driver_params verbatim (so an explicit
    # driver_params value still wins). All FLAT under the /** wildcard node.
    merged: dict = {}
    merged.update(derive_connection(connection))
    for key, value in (cfg.get("driver_params") or {}).items():
        merged[key] = value
    doc = {"/**": {"ros__parameters": merged}}
    yaml.safe_dump(doc, sys.stdout, default_flow_style=False, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
