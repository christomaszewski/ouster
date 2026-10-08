import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for filename in ("ouster-up", "tools/render_params.py", "sensors/ouster.example.yaml",
                         "docker/compose/compose.deploy.yaml"):
            target = self.root / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / filename, target)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text('''#!/usr/bin/env bash
echo "${OUSTER_TARGET_STATE:-unset} docker $*" >> "$ARGV_LOG"
echo "shutdown=${OUSTER_SHUTDOWN_STATE:-unset}" >> "$ARGV_LOG"
case "$*" in
  "compose version") exit 0 ;;
  "inspect --format"*) echo "${STOP_EXIT_CODE:-0}" ;;
  "logs --tail 40"*) echo 'shutdown log evidence' ;;
  *" ps -q driver"|*" ps -aq driver") [ "${RUNNING:-}" = 1 ] && echo container; exit 0 ;;
  *" exec -T driver /usr/local/bin/statectl.sh state")
    [ "${UNREACHABLE:-}" = 1 ] && exit 1
    value="$(cat "$FAKE_STATE")"
    printf '{"state": "%s", "detail": "%s"}\\n' "$value" "${FAKE_DETAIL:-fake}" ;;
  *" exec -T driver /usr/local/bin/statectl.sh activate") echo active > "$FAKE_STATE" ;;
  *" exec -T driver /usr/local/bin/statectl.sh standby") echo standby > "$FAKE_STATE" ;;
esac
exit 0
''')
        docker.chmod(0o755)
        self.config = self.root / "sensors/ouster.example.yaml"
        self.log = self.root / "argv"
        self.state = self.root / "state"
        self.state.write_text("standby")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OUSTER_", "RIG_", "COMPOSE_"))}
        self.env.update(PATH=str(self.bin) + ":" + self.env["PATH"], ARGV_LOG=str(self.log),
                        FAKE_STATE=str(self.state), OUSTER_SETTLE_TIMEOUT="1")

    def run_launcher(self, *args, **env):
        return subprocess.run([str(self.root / "ouster-up"), str(self.config), *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=20)

    def standby_config(self):
        self.config.write_text(self.config.read_text().replace("# initial_state: standby", "initial_state: standby"))

    def test_down_state_is_json_and_never_renders(self):
        result = self.run_launcher("state")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "down")
        self.assertFalse((self.root / "var").exists())
        self.assertNotIn("volume create", self.log.read_text())

    def test_transitions_do_not_create_containers(self):
        for verb in ("standby", "activate"):
            result = self.run_launcher(verb)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
        self.assertNotIn("volume create", self.log.read_text())
        self.assertNotIn(" up", self.log.read_text())

    def test_initial_standby_reaches_container_before_up(self):
        self.standby_config()
        result = self.run_launcher("up", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("standby docker compose", self.log.read_text())
        self.assertNotIn("statectl.sh activate", self.log.read_text())

    def test_foreground_standby_is_supported(self):
        self.standby_config()
        result = self.run_launcher("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("standby docker compose", self.log.read_text())

    def test_active_override_converges_existing_parked_container(self):
        self.standby_config()
        result = self.run_launcher("up", "-d", RIG_TARGET_STATE="active", OUSTER_SETTLE_TIMEOUT="10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text().strip(), "active")
        self.assertIn("active docker compose", self.log.read_text())
        self.assertIn("statectl.sh activate", self.log.read_text())

    def test_unreachable_startup_times_out(self):
        result = self.run_launcher("up", "-d", UNREACHABLE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not reach", result.stderr)

    def test_up_can_replace_a_failing_runtime_target(self):
        self.standby_config()
        self.state.write_text("transitioning")
        result = self.run_launcher("up", "-d", FAKE_DETAIL="target:active; sensor disconnected",
                                   OUSTER_SETTLE_TIMEOUT="10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text().strip(), "standby")

    def test_invalid_target_is_rejected(self):
        self.assertNotEqual(self.run_launcher("up", "-d", RIG_TARGET_STATE="parked").returncode, 0)

    def test_driver_standby_mode_is_rejected(self):
        self.config.write_text(self.config.read_text().replace("driver_params:", "driver_params:\n  operating_mode: STANDBY\n#"))
        result = self.run_launcher("up", "-d")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operating_mode", result.stderr)

    def test_shutdown_defaults_to_standby(self):
        result = self.run_launcher("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shutdown=standby", self.log.read_text())

    def test_shutdown_unchanged_is_forwarded(self):
        self.config.write_text(self.config.read_text().replace("shutdown_state: standby", "shutdown_state: unchanged"))
        result = self.run_launcher("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shutdown=unchanged", self.log.read_text())

    def test_invalid_shutdown_setting_never_calls_compose(self):
        self.config.write_text(self.config.read_text().replace("shutdown_state: standby", "shutdown_state: active"))
        result = self.run_launcher("up", "-d", SERVICE="ouster", NAME="top",
                                   NAMESPACE="/top", INITIAL_STATE="active",
                                   ZENOH_OVERRIDE="", SHUTDOWN_STATE="standby")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shutdown_state", result.stderr)
        self.assertNotIn("volume create", self.log.read_text())
        self.assertNotIn(" up", self.log.read_text())

    def test_inspection_and_teardown_never_render_or_damage_params(self):
        for verb in ("status", "logs", "down"):
            result = self.run_launcher(verb)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "var").exists(), verb)
        self.assertEqual(self.run_launcher("config").returncode, 0)
        original = {p.name: p.read_bytes() for p in (self.root / "var/run").glob("*.yaml")}
        self.config.write_text(self.config.read_text().replace("driver_params:", "driver_params:\n  operating_mode: STANDBY\n#"))
        for verb in ("status", "logs", "down"):
            result = self.run_launcher(verb)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.run_launcher("up").returncode, 0)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "var/run").glob("*.yaml")}, original)

    def test_param_changes_select_new_mount_without_overwriting_old_file(self):
        self.assertEqual(self.run_launcher("up").returncode, 0)
        old = next((self.root / "var/run").glob("*.yaml"))
        content = old.read_bytes()
        self.config.write_text(self.config.read_text().replace('"1024x10"', '"512x10"'))
        self.assertEqual(self.run_launcher("up").returncode, 0)
        self.assertEqual(old.read_bytes(), content)
        self.assertEqual(len(list((self.root / "var/run").glob("*.yaml"))), 2)

    def test_down_checks_container_exit_before_removing_logs(self):
        result = self.run_launcher("down", RUNNING="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertLess(calls.index(" stop driver"), calls.index("inspect --format"))
        self.assertLess(calls.index("inspect --format"), calls.index(" down"))

    def test_failed_shutdown_is_reported_even_when_compose_down_succeeds(self):
        result = self.run_launcher("down", RUNNING="1", STOP_EXIT_CODE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sensor state unconfirmed", result.stderr)
        self.assertIn("shutdown log evidence", result.stderr)
        self.assertIn(" down", self.log.read_text())

    def test_down_forwards_user_timeout_to_stop(self):
        result = self.run_launcher("down", "--timeout", "15", RUNNING="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" stop --timeout 15 driver", self.log.read_text())
        self.assertIn(" down --timeout 15", self.log.read_text())

    def test_connection_type_cannot_execute_shell_commands(self):
        marker = self.root / "injected"
        self.config.write_text(self.config.read_text().replace(
            "type: lidar", f"type: 'lidar; touch {marker}'"))
        result = self.run_launcher("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertNotIn("volume create", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
