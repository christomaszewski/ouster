#!/usr/bin/env python3
"""Own sensor mode and the upstream driver process within one running container.

Only the supervisor writes sensor config or starts/stops the driver. Commands
arrive over a local Unix socket; state/health inspect both physical and ROS state.
"""

import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

import yaml

from sensor_http import SensorHTTP
from temperature import temperature_monitor

LOG = logging.getLogger("ouster-state")
SOCKET = os.environ.get("OUSTER_STATE_SOCKET", "/run/ouster/state.sock")
TARGET_FILE = os.environ.get("OUSTER_TARGET_FILE", "/var/lib/ouster/target-state")
PARAMS_FILE = os.environ.get("OUSTER_PARAMS_FILE", "/etc/ouster_driver/params.yaml")


class Driver:
    def __init__(self, params_file, namespace):
        self.command = ["ros2", "launch", "ouster_ros", "driver.launch.py",
                        f"params_file:={params_file}", f"ouster_ns:={namespace}", "viz:=false"]
        self.node = namespace.rstrip("/") + "/os_driver"
        self.process = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def lifecycle(self):
        if not self.running():
            return "absent"
        try:
            result = subprocess.run(
                ["ros2", "lifecycle", "get", self.node], capture_output=True,
                text=True, timeout=5, check=True,
            )
            # Zenoh can emit ANSI-colored watchdog warnings on stdout before or after
            # the CLI response. Only accept the lifecycle response's complete line.
            output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
            for line in output.splitlines():
                match = re.fullmatch(r"([a-z][a-z0-9_]*)\s+\[\d+\]", line.strip())
                if match:
                    return match.group(1)
            return "unreachable"
        except subprocess.SubprocessError:
            return "unreachable"

    def start(self):
        if self.process is not None:
            raise RuntimeError("old driver process must be stopped before starting")
        self.process = subprocess.Popen(self.command, start_new_session=True)

    def stop(self):
        if self.process is None:
            return
        # Drain both the UDP reader (deactivate) and scan-processing thread
        # (cleanup) while the ROS context is still usable. SIGINT alone closes
        # Zenoh before these threads finish publishing and can abort os_driver.
        if self.running():
            try:
                state = self.lifecycle()
                transitions = {"active": ("deactivate", "cleanup"),
                               "inactive": ("cleanup",)}.get(state, ())
                for transition in transitions:
                    subprocess.run(
                        ["ros2", "lifecycle", "set", self.node, transition],
                        capture_output=True, text=True, timeout=5, check=True,
                    )
            except (OSError, subprocess.SubprocessError) as error:
                LOG.warning("driver lifecycle cleanup unavailable; stopping process group: %s", error)
        # Kill/wait for the entire launch process group, even if its leader has
        # already exited. No upstream config writer may survive into standby.
        pgid = self.process.pid
        for sig, budget in ((signal.SIGINT, 8), (signal.SIGTERM, 4), (signal.SIGKILL, 4)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + budget
            while time.monotonic() < deadline:
                self.process.poll()  # reap the leader; Docker init reaps descendants
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    self.process.wait()
                    self.process = None
                    return
                time.sleep(0.1)
        else:
            raise RuntimeError("driver process group did not stop; sensor mode unchanged")
        self.process.wait()
        self.process = None


class Controller:
    def __init__(self, sensor, driver, initial, target_file, *, clock=time.monotonic,
                 stop_event=None):
        if initial not in ("active", "standby"):
            raise ValueError("initial state must be active or standby")
        self.sensor = sensor
        self.driver = driver
        self.target_file = Path(target_file)
        self.target = self.target_file.read_text().strip() if self.target_file.exists() else initial
        if self.target not in ("active", "standby"):
            raise ValueError(f"invalid saved target state: {self.target!r}")
        self.clock = clock
        self.stop_event = stop_event or threading.Event()
        self.lock = threading.Lock()
        self.changing = threading.Event()
        self.error = ""

    def save_target(self, target):
        self.target_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.target_file.with_suffix(".tmp")
        temporary.write_text(target + "\n")
        temporary.replace(self.target_file)
        self.target = target

    def pause(self, seconds):
        if self.stop_event.wait(seconds):
            raise RuntimeError("supervisor stopping")

    def observe(self):
        info, config = self.sensor.snapshot()
        lifecycle = self.driver.lifecycle()
        mode, status = config["operating_mode"], info.get("status", "unknown")
        state = "transitioning"
        if self.target == "standby" and self.driver.process is None and mode == "STANDBY" and status == "STANDBY":
            state = "standby"
        elif self.target == "active" and lifecycle == "active" and mode == "NORMAL" and status == "RUNNING":
            state = "active"
        detail = f"target:{self.target}; lifecycle:{lifecycle}; sensor:{mode}/{status}"
        return {"state": state, "detail": detail}

    def state(self):
        if self.changing.is_set():
            return {"state": "transitioning", "detail": f"target:{self.target}; transition in progress"}
        try:
            result = self.observe()
        except Exception as error:
            result = {"state": "transitioning", "detail": f"target:{self.target}; {error}"}
        if self.changing.is_set():
            return {"state": "transitioning", "detail": f"target:{self.target}; transition in progress"}
        if result["state"] == "transitioning" and self.error:
            result["detail"] += f"; last error:{self.error}"
        return result

    def reconcile(self):
        if self.stop_event.is_set():
            raise RuntimeError("supervisor stopping")
        try:
            if self.observe()["state"] == self.target:
                return
        except Exception:
            pass  # stop upstream recovery before attempting sensor recovery
        self.changing.set()
        self.driver.stop()
        if self.stop_event.is_set():
            raise RuntimeError("supervisor stopping")
        mode = "NORMAL" if self.target == "active" else "STANDBY"
        LOG.info("waiting for sensor %s", mode)
        self.sensor.ensure_mode(mode, stop_event=self.stop_event)
        if self.target == "active":
            if self.stop_event.is_set():
                raise RuntimeError("supervisor stopping")
            self.driver.start()
            deadline = self.clock() + 90
            try:
                while self.clock() < deadline:
                    if not self.driver.running():
                        raise RuntimeError("driver exited during startup")
                    try:
                        if self.observe()["state"] == "active":
                            break
                    except Exception:
                        pass  # sensor may reinitialize when driver config is applied
                    self.pause(2)
                else:
                    raise RuntimeError("driver did not become active within 90 seconds")
            except Exception:
                self.driver.stop()
                raise
        # Confirm both layers after the transition, not a snapshot from before it.
        result = self.observe()
        if result["state"] != self.target:
            raise RuntimeError(f"transition did not settle: {result['detail']}")
        LOG.info("%s complete: %s", self.target, result["detail"])

    def transition(self, target=None):
        if target is not None and target not in ("active", "standby"):
            raise ValueError("target must be active or standby")
        # Allow a brief status probe to finish, but do not queue a command
        # indefinitely behind another transition.
        if not self.lock.acquire(timeout=20 if target is not None else 0):
            raise RuntimeError("transition in progress; retry when state settles")
        try:
            if self.stop_event.is_set():
                raise RuntimeError("supervisor stopping")
            if target is not None:
                self.save_target(target)
            self.reconcile()
            self.error = ""
        except Exception as error:
            self.error = str(error)
            raise
        finally:
            self.changing.clear()
            self.lock.release()

    def shutdown(self, target):
        """Stop all config writers, then park without changing the restart target."""
        if target not in ("standby", "unchanged"):
            raise ValueError("shutdown state must be standby or unchanged")
        self.stop_event.set()
        self.changing.set()
        # Leave margin inside Compose's 120-second stop grace period. An
        # interrupted transition can still be reaping the driver process group.
        deadline = self.clock() + 100
        if not self.lock.acquire(timeout=55):
            raise RuntimeError("shutdown could not acquire transition lock; sensor state unconfirmed")
        try:
            self.driver.stop()
            if target == "standby":
                remaining = min(60, deadline - self.clock())
                if remaining <= 0:
                    raise RuntimeError("shutdown timed out before parking sensor")
                LOG.info("shutdown: waiting for sensor STANDBY")
                # This final transition must run despite stop_event. It neither
                # starts the driver nor persists a new target or sensor config.
                self.sensor.ensure_mode("STANDBY", timeout=remaining)
                LOG.info("shutdown complete: sensor STANDBY; restart target:%s", self.target)
            else:
                LOG.info("shutdown complete: sensor mode unchanged; restart target:%s", self.target)
        except Exception:
            LOG.error("shutdown failed; sensor standby is NOT confirmed")
            raise
        finally:
            self.lock.release()


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            command = json.loads(self.rfile.readline(1024))["command"]
            controller = self.server.controller
            if command in ("standby", "activate"):
                controller.transition("active" if command == "activate" else "standby")
                result = {"ok": True}
            elif command == "state":
                result = {"ok": True, **controller.state()}
            else:
                raise ValueError(f"unknown command: {command}")
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        try:
            self.wfile.write((json.dumps(result) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


def load_sensor_params(params_file):
    with open(params_file) as stream:
        document = yaml.safe_load(stream) or {}
    for node in document.values():
        params = (node or {}).get("ros__parameters", {})
        if params.get("operating_mode") not in (None, "", "NORMAL"):
            raise ValueError("driver_params.operating_mode must be omitted or NORMAL; use initial_state instead")
        if params.get("sensor_hostname"):
            return params
    raise ValueError(f"no sensor_hostname in {params_file}")


def serve(*, monitor=temperature_monitor):
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    namespace = os.environ.get("OUSTER_NAMESPACE", "ouster")
    shutdown_state = os.environ.get("OUSTER_SHUTDOWN_STATE", "standby")
    if shutdown_state not in ("standby", "unchanged"):
        raise ValueError("OUSTER_SHUTDOWN_STATE must be standby or unchanged")
    params = load_sensor_params(PARAMS_FILE)
    driver = Driver(PARAMS_FILE, namespace)
    controller = Controller(SensorHTTP(str(params["sensor_hostname"])), driver,
                            os.environ.get("OUSTER_TARGET_STATE", "active"), TARGET_FILE,
                            stop_event=stop_event)
    path = Path(SOCKET)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink(missing_ok=True)
        with Server(str(path), Handler) as server:
            os.chmod(path, 0o600)
            server.controller = controller
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            shutdown_started = False

            def shutdown():
                nonlocal shutdown_started
                if shutdown_started:
                    return
                shutdown_started = True
                stop_event.set()
                server.shutdown()
                thread.join()
                controller.shutdown(shutdown_state)

            try:
                with monitor(controller.sensor, namespace, params.get("sensor_frame", "os_sensor"),
                             stop_event, controller.changing):
                    try:
                        while not stop_event.is_set():
                            try:
                                controller.transition()
                            except Exception as error:
                                LOG.warning("%s", error)
                            stop_event.wait(5)
                    finally:
                        # Park before closing the temperature ROS context too.
                        shutdown()
            finally:
                try:
                    # Also park if initializing the optional ROS monitor fails.
                    shutdown()
                finally:
                    path.unlink(missing_ok=True)


def client(command, *, output=True):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(300 if command in ("standby", "activate") else 15)
        connection.connect(SOCKET)
        connection.sendall((json.dumps({"command": command}) + "\n").encode())
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline(65536))
    if not result.pop("ok"):
        raise RuntimeError(result["error"])
    if command == "state" and output:
        print(json.dumps(result))
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        if command == "serve":
            serve()
        elif command == "health":
            return 0 if client("state", output=False)["state"] in ("active", "standby") else 1
        elif command in ("state", "standby", "activate"):
            client(command)
        else:
            raise ValueError("usage: operational_state.py serve|state|standby|activate|health")
        return 0
    except Exception as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
