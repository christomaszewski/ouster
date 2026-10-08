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
upstream's flat connection params (sensor_hostname / udp_dest / lidar_port / imu_port). The
optional `zenoh:` block never reaches the params doc: it is validated here, rendered to ONE
ZENOH_CONFIG_OVERRIDE pair string, and emitted (`--env` mode) for the entrypoint's in-container
export. Used by `ouster-up`:

  render_params.py <config.yaml>          # -> the ROS 2 params YAML on stdout
  render_params.py --env <config.yaml>    # -> identity, startup/shutdown state, Zenoh env lines

The params doc is keyed by the `/**` wildcard node so it binds regardless of the namespace rig
pushes via `ouster_ns`. (Upstream's own driver_params.yaml keys the explicit node path
`ouster/os_driver`, which would NOT match a custom namespace -- so do NOT copy that keying.)
Needs PyYAML on the host (apt: python3-yaml).
"""
import json
import re
import shlex
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


def derive_zenoh(zenoh) -> tuple:
    """Validate the OPTIONAL top-level `zenoh:` block -> (override_pairs, warnings).

    Fleet-uniform knob -- the rig-infra ros2-bag-logger takes the IDENTICAL block; keep the shape
    AND the flattening in sync with its tools/bag_cmd.py zenoh_env_lines(). Renders one
    ZENOH_CONFIG_OVERRIDE "path=json;..." pair string ("" when the block sets nothing): rmw_zenoh
    applies the pairs ON TOP of whatever session config it loads (its shipped ROS default, or a
    deployment-set ZENOH_SESSION_CONFIG_URI file), so untouched keys keep their loaded values.
    `shared_memory`/`shm_pool_mb` are sugar for the two SHM keys; `overrides` is any zenoh
    session-config subtree, flattened to leaf pairs AFTER the sugar (later pairs win in
    rmw_zenoh, so explicit overrides beat the sugar). Exported by the image's entrypoint ONLY
    under rmw_zenoh_cpp -- inert under Fast DDS. Unknown keys are a HARD error: unmet SHM
    preconditions fall back to TCP loopback silently, so a typo here must not pass as
    "configured".
    """
    if not isinstance(zenoh, dict):
        sys.stderr.write("render_params: `zenoh:` must be a mapping\n")
        sys.exit(2)
    unknown = sorted(set(zenoh) - {"shared_memory", "shm_pool_mb", "overrides"})
    if unknown:
        sys.stderr.write(f"render_params: unknown zenoh key(s): {', '.join(unknown)} "
                         "(supported: shared_memory, shm_pool_mb, overrides)\n")
        sys.exit(2)
    enable = zenoh.get("shared_memory", False)
    if not isinstance(enable, bool):
        sys.stderr.write("render_params: zenoh.shared_memory must be a bool\n")
        sys.exit(2)
    pool_mb = zenoh.get("shm_pool_mb")
    if pool_mb is not None and (isinstance(pool_mb, bool)
                                or not isinstance(pool_mb, int) or pool_mb <= 0):
        sys.stderr.write("render_params: zenoh.shm_pool_mb must be a positive integer (MB)\n")
        sys.exit(2)
    warns = []
    if pool_mb is not None and not enable:
        warns.append("zenoh.shm_pool_mb is ignored without zenoh.shared_memory: true")
        pool_mb = None

    pairs = []
    if enable:
        pairs.append(("transport/shared_memory/enabled", "true"))
        if pool_mb:
            pairs.append(("transport/shared_memory/transport_optimization/pool_size",
                          str(pool_mb * 1024 * 1024)))
    overrides = zenoh.get("overrides")
    if overrides is not None and not isinstance(overrides, dict):
        sys.stderr.write("render_params: zenoh.overrides must be a mapping "
                         "(nested zenoh session config)\n")
        sys.exit(2)
    if overrides:
        def leaves(prefix, val):
            if isinstance(val, dict) and val:
                for key in sorted(val):
                    leaves(f"{prefix}/{key}" if prefix else str(key), val[key])
            else:
                pairs.append((prefix, json.dumps(val, sort_keys=True, separators=(",", ":"))))
        leaves("", overrides)
    for path, value in pairs:
        if ";" in path or ";" in value:
            sys.stderr.write(f"render_params: zenoh override at {path!r} contains ';' -- "
                             "rmw_zenoh's ZENOH_CONFIG_OVERRIDE parser splits on ';'\n")
            sys.exit(2)
    return ";".join(f"{p}={v}" for p, v in pairs), warns


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
    zenoh_pairs, zenoh_warns = derive_zenoh(cfg.get("zenoh") or {})

    # Operational-state contract: the state `up` leaves the instance in. Absent = active
    # (existing configs untouched); the launcher applies the RIG_TARGET_STATE > initial_state
    # > active precedence. Hard error on anything else — a typo parking (or waking) a sensor
    # must not pass silently.
    initial_state = str(cfg.get("initial_state") or "active")
    if initial_state not in ("standby", "active"):
        sys.stderr.write(f"render_params: initial_state {initial_state!r} "
                         "must be 'standby' or 'active'\n")
        sys.exit(2)

    shutdown_state = cfg.get("shutdown_state", "standby")
    if shutdown_state not in ("standby", "unchanged"):
        sys.stderr.write(f"render_params: shutdown_state {shutdown_state!r} "
                         "must be 'standby' or 'unchanged'\n")
        return 2

    if env_mode:
        # warn only where the knob is CONSUMED (params mode never reads `zenoh:`), so one
        # ouster-up run -- which invokes both modes -- surfaces each warning exactly once
        for warn in zenoh_warns:
            sys.stderr.write("render_params: " + warn + "\n")
        # identity tokens are ident-safe by construction; the override pair string is NOT (JSON
        # quotes/brackets/spaces), so shlex-quote it for the launcher's eval'd `export` block.
        # Both state settings include defaults so stale env values cannot leak in.
        lines = [f"SERVICE={service}", f"NAME={name}", f"NAMESPACE=/{namespace}",
                 "TYPE=" + shlex.quote(ttype),
                 f"INITIAL_STATE={initial_state}",
                 f"SHUTDOWN_STATE={shutdown_state}",
                 "ZENOH_OVERRIDE=" + shlex.quote(zenoh_pairs)]
        print("\n".join(lines))
        return 0

    # params mode: connection-derived keys first, then driver_params verbatim (so an explicit
    # driver_params value still wins). All FLAT under the /** wildcard node.
    merged: dict = {}
    merged.update(derive_connection(connection))
    for key, value in (cfg.get("driver_params") or {}).items():
        merged[key] = value
    if merged.get("operating_mode") not in (None, "", "NORMAL"):
        sys.stderr.write("render_params: omit driver_params.operating_mode or use NORMAL; "
                         "select standby with initial_state instead\n")
        return 2
    doc = {"/**": {"ros__parameters": merged}}
    yaml.safe_dump(doc, sys.stdout, default_flow_style=False, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
