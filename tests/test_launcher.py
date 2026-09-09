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
case "$*" in
  "compose version") exit 0 ;;
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


if __name__ == "__main__":
    unittest.main()
