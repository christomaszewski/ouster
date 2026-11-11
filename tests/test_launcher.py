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
echo "health=${OUSTER_HEALTH_CONFIG:-unset}" >> "$ARGV_LOG"
echo "mask=${OUSTER_MASK_HOST:-unset}" >> "$ARGV_LOG"
case "$*" in
  "compose version") exit 0 ;;
  "inspect --format {{.State.Status}}"*) echo "${CONTAINER_STATUS:-running}" ;;
  "inspect --format"*) echo "${STOP_EXIT_CODE:-0}" ;;
  "logs --tail 40"*) echo 'shutdown log evidence' ;;
  *" stop "*driver) [ "${STOP_FAILS:-}" = 1 ] && exit 9 ;;
  *" ps -q driver"|*" ps -aq driver") [ "${RUNNING:-}" = 1 ] && echo container; exit 0 ;;
  *" exec -T driver /usr/local/bin/statectl.sh state")
    [ "${UNREACHABLE:-}" = 1 ] && exit 1
    value="$(cat "$FAKE_STATE")"
    printf '{"state": "%s", "detail": "%s"}\\n' "$value" "${FAKE_DETAIL:-fake}" ;;
  *" exec -T driver /usr/local/bin/statectl.sh activate")
    [ "${UNREACHABLE:-}" = 1 ] && exit 1
    sleep "${ACTIVATE_SECONDS:-0}"
    echo active > "$FAKE_STATE" ;;
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

    def test_state_forwards_the_supervisors_answer(self):
        result = self.run_launcher("state", RUNNING="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"state": "standby", "detail": "fake"})

    def test_state_answers_while_the_supervisor_is_unreachable(self):
        """A listed container whose supervisor does not answer: still exit 0 and one JSON object
        from the contract's vocabulary, never an empty stdout (rig then shows "unknown")."""
        for status, expected in (("running", {"state": "transitioning", "detail": "supervisor unreachable"}),
                                 ("restarting", {"state": "down", "detail": "container restarting"}),
                                 ("exited", {"state": "down", "detail": "container exited"}),
                                 ('"; bad', {"state": "down", "detail": "container not running"})):
            with self.subTest(status=status):
                result = self.run_launcher("state", RUNNING="1", UNREACHABLE="1", CONTAINER_STATUS=status)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertIn("did not answer", result.stderr)
        self.assertFalse((self.root / "var").exists())

    def test_transition_through_an_unreachable_supervisor_still_fails(self):
        result = self.run_launcher("activate", RUNNING="1", UNREACHABLE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

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

    def test_transition_that_outlasts_the_settle_budget_and_succeeds_is_a_success(self):
        result = self.run_launcher("up", "-d", ACTIVATE_SECONDS="2", OUSTER_SETTLE_TIMEOUT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text().strip(), "active")

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

    def test_health_config_reaches_runtime_without_becoming_a_ros_parameter(self):
        self.config.write_text(self.config.read_text() + "\nhealth: {poll_interval_s: 7, limits: {supply.power_w: {warn_above: 30}}}\n")
        result = self.run_launcher("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"poll_interval_s": 7', self.log.read_text())
        for params in (self.root / "var/run").glob("*.yaml"):
            self.assertNotIn("poll_interval_s", params.read_text())

    def test_invalid_health_config_never_starts_a_container(self):
        self.config.write_text(self.config.read_text() + "\nhealth: {poll_interval_s: 0}\n")
        result = self.run_launcher("up", "-d")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("health.poll_interval_s", result.stderr)
        self.assertNotIn(" up", self.log.read_text())

    def test_bare_health_key_means_defaults(self):
        self.config.write_text(self.config.read_text() + "\nhealth:\n  # interval_s: 1\n")
        result = self.run_launcher("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("health={}", self.log.read_text())
        self.config.write_text(self.config.read_text() + "\nhealth: false\n")
        result = self.run_launcher("config")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("health must be a mapping", result.stderr)

    def test_missing_sensor_address_never_starts_a_container(self):
        text = self.config.read_text()
        self.assertIn("    sensor_hostname: 192.168.1.50", text)
        self.config.write_text(text.replace("    sensor_hostname: 192.168.1.50", "    # sensor_hostname:"))
        for args in (("up", "-d"), ("up",), ("config",)):
            with self.subTest(args=args):
                result = self.run_launcher(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("sensor_hostname is required", result.stderr)
        calls = self.log.read_text()
        self.assertNotIn("volume create", calls)
        self.assertNotIn(" up", calls)
        self.assertFalse((self.root / "var").exists())
        for verb in ("status", "logs", "down", "state"):  # a broken config can still be inspected and removed
            with self.subTest(verb=verb):
                self.assertEqual(self.run_launcher(verb).returncode, 0)

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

    def test_failed_stop_still_tears_down_and_is_reported(self):
        result = self.run_launcher("down", RUNNING="1", STOP_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopping the driver container failed", result.stderr)
        calls = self.log.read_text()
        self.assertLess(calls.index(" stop driver"), calls.index(" down"))

    def test_down_forwards_user_timeout_to_stop(self):
        result = self.run_launcher("down", "--timeout", "15", RUNNING="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" stop --timeout 15 driver", self.log.read_text())
        self.assertIn(" down --timeout 15", self.log.read_text())

    PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"

    def use_mask(self, path, content=PNG, written_as=None):
        """Put an image at `path` and name it in the config's mask block."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.config.write_text(self.config.read_text() + f"\nmask:\n  path: {written_as or path.name}\n")

    def rendered_params(self):
        return [p.read_text() for p in (self.root / "var/run").glob("*.params.yaml")]

    def test_no_mask_mounts_nothing_and_sets_no_mask_path(self):
        self.assertEqual(self.run_launcher("up").returncode, 0)
        self.assertIn("mask=unset", self.log.read_text())
        self.assertNotIn("mask_path", "".join(self.rendered_params()))
        self.assertEqual(list((self.root / "var/run").glob("*.mask")), [])

    def test_mask_beside_the_config_is_copied_and_mounted(self):
        self.use_mask(self.config.parent / "front.png")
        result = self.run_launcher("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = list((self.root / "var/run").glob("*.mask"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), self.PNG)
        self.assertIn(f"mask={copies[0]}", self.log.read_text())
        self.assertIn("mask_path: /etc/ouster_driver/mask", "".join(self.rendered_params()))
        self.assertIn(f"mask={self.config.parent / 'front.png'}", result.stderr)

    def test_mask_written_from_the_deployment_root_is_found_from_a_rendered_config(self):
        # rig hands the launcher either config/sensors/<name>.yaml or its render under var/rendered/
        self.use_mask(self.root / "config/sensors/front.png", written_as="config/sensors/front.png")
        for place in ("config/sensors/ouster.yaml", "var/rendered/ouster.yaml"):
            with self.subTest(config=place):
                config = self.root / place
                config.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / "sensors/ouster.example.yaml", config)
                result = subprocess.run([str(self.root / "ouster-up"), str(config), "config"],
                                        env=self.env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.root / "var/run").glob("*.mask"))), 1)

    def test_absolute_mask_path_is_taken_as_written(self):
        elsewhere = Path(tempfile.mkdtemp(dir=self.root)) / "mask.bmp"
        self.use_mask(elsewhere, content=b"BM" + b"pixels", written_as=str(elsewhere))
        self.assertEqual(self.run_launcher("up").returncode, 0)
        self.assertEqual(len(list((self.root / "var/run").glob("*.mask"))), 1)

    def test_missing_mask_never_starts_a_container(self):
        self.config.write_text(self.config.read_text() + "\nmask:\n  path: nowhere/front.png\n")
        for args in (("up", "-d"), ("up",), ("config",)):
            with self.subTest(args=args):
                result = self.run_launcher(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("mask.path 'nowhere/front.png': no such file", result.stderr)
        calls = self.log.read_text()
        self.assertNotIn("volume create", calls)
        self.assertNotIn(" up", calls)
        self.assertFalse((self.root / "var").exists())
        for verb in ("status", "logs", "down", "state"):  # still inspectable and removable
            with self.subTest(verb=verb):
                self.assertEqual(self.run_launcher(verb).returncode, 0)

    def test_file_the_driver_cannot_read_is_refused_before_startup(self):
        webp = b"RIFF\x1a\x00\x00\x00WEBPVP8L"
        for name, content, message in (("front.webp", webp, "is not an image the driver reads"),
                                       ("front.png", b"mask_path: text", "is not an image the driver reads"),
                                       ("empty.png", b"", "is empty")):
            with self.subTest(name=name):
                original = self.config.read_text()
                self.use_mask(self.config.parent / name, content=content)
                result = self.run_launcher("up")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.config.write_text(original)
        self.assertNotIn(" up", self.log.read_text())

    def test_tga_has_no_signature_and_is_accepted_by_name(self):
        self.use_mask(self.config.parent / "front.TGA", content=bytes(18) + b"pixels")
        self.assertEqual(self.run_launcher("up").returncode, 0)

    def test_redrawn_mask_selects_a_new_mount_and_keeps_the_old_copy(self):
        mask = self.config.parent / "front.png"
        self.use_mask(mask)
        self.assertEqual(self.run_launcher("up").returncode, 0)
        old = next((self.root / "var/run").glob("*.mask"))
        mask.write_bytes(self.PNG + b" redrawn")
        self.assertEqual(self.run_launcher("up").returncode, 0)
        copies = sorted((self.root / "var/run").glob("*.mask"))
        self.assertEqual(len(copies), 2)
        self.assertEqual(old.read_bytes(), self.PNG)
        self.assertIn(f"mask={next(c for c in copies if c != old)}", self.log.read_text())
        self.assertEqual(len(self.rendered_params()), 1)  # same params: only the mount changed

    def test_inspection_and_teardown_never_copy_the_mask(self):
        self.use_mask(self.config.parent / "front.png")
        for verb in ("status", "logs", "down", "state"):
            result = self.run_launcher(verb)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "var").exists(), verb)

    def test_mask_block_and_driver_mask_path_cannot_both_be_set(self):
        text = self.config.read_text()
        self.assertIn('  # mask_path: ""', text)
        self.config.write_text(text.replace('  # mask_path: ""', "  mask_path: /root/.ros/mask.png #"))
        self.use_mask(self.config.parent / "front.png")
        result = self.run_launcher("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("set either mask.path", result.stderr)
        self.assertNotIn(" up", self.log.read_text())

    def test_malformed_mask_block_is_refused_but_can_still_be_torn_down(self):
        for block, message in (("mask: front.png", "must be a mapping"),
                               ("mask:\n  file: front.png", "unknown mask key(s): file"),
                               ("mask:\n  path: ''", "mask.path must name an image file")):
            with self.subTest(block=block):
                original = self.config.read_text()
                self.config.write_text(original + "\n" + block + "\n")
                result = self.run_launcher("up")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.run_launcher("down").returncode, 0)
                self.config.write_text(original)
        self.assertNotIn(" up", self.log.read_text())

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
