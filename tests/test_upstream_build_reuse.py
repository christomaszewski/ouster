"""Exercise the regression runner's actual reuse guard without ROS or Docker."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UpstreamBuildReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Execute the inline program shipped in the runner, not a copy of its
        # fingerprint logic. Keep the runner self-contained in the Docker stage.
        script = (ROOT / "tools/test_upstream.sh").read_text()
        match = re.search(r"check_build_reuse\(\) \{.*?<<'PY'\n(.*?)\nPY\n\}",
                          script, re.DOTALL)
        if match is None:
            raise AssertionError("Regression runner's reuse guard is missing")
        cls.program = match.group(1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.build = self.root / "build"
        self.install = self.root / "install"
        for directory in (self.source, self.build, self.install):
            directory.mkdir()
        self.cache = self.build / "CMakeCache.txt"
        self.cache.write_text(
            "CMAKE_BUILD_TYPE:STRING=Release\n"
            f"CMAKE_HOME_DIRECTORY:INTERNAL={self.source}\n"
            "CMAKE_GENERATOR:INTERNAL=Ninja\n"
            "CMAKE_CXX_COMPILER_LAUNCHER:STRING=ccache\n")
        self.obj = self.build / "sensor.o"
        self.obj.write_bytes(b"production object")
        (self.install / "setup.bash").write_text("# fixture\n")
        self.library = self.install / "libdriver.so"
        self.library.write_bytes(b"production artifact")
        self.link = self.install / "driver.so"
        self.link.symlink_to(self.library.name)
        self.report = self.root / "build-reuse.json"

    def run_guard(self, phase, error=None):
        result = subprocess.run(
            [sys.executable, "-c", self.program, phase, str(self.build),
             str(self.source), str(self.install), str(self.report)],
            capture_output=True, text=True)
        if error is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error, result.stderr)
        return result

    def test_reuses_production_objects_while_allowing_new_test_objects(self):
        self.run_guard("snapshot")
        (self.build / "test.o").write_bytes(b"new regression object")
        self.run_guard("verify")
        self.assertEqual(json.loads(self.report.read_text())["verification"], {
            "production_objects_reused": 1,
            "production_install_unchanged": True,
        })

    def test_rejects_recompiled_object_even_with_identical_bytes(self):
        self.run_guard("snapshot")
        os.utime(self.obj, ns=(1, 1))
        self.run_guard("verify", "Production objects were rebuilt")

    def test_rejects_missing_production_object(self):
        self.run_guard("snapshot")
        self.obj.unlink()
        self.run_guard("verify", "Production objects were rebuilt")

    def test_rejects_changed_install_content(self):
        self.run_guard("snapshot")
        self.library.write_bytes(b"changed artifact")
        self.run_guard("verify", "changed production install")

    def test_rejects_new_install_files(self):
        self.run_guard("snapshot")
        (self.install / "test-binary").write_bytes(b"accidentally installed")
        self.run_guard("verify", "changed production install")

    def test_rejects_changed_install_symlink(self):
        self.run_guard("snapshot")
        self.link.unlink()
        self.link.symlink_to("different-library.so")
        self.run_guard("verify", "changed production install")

    def test_rejects_changed_compiler_launcher(self):
        self.run_guard("snapshot")
        self.cache.write_text(self.cache.read_text().replace("ccache", "other"))
        self.run_guard("verify", "changed production compiler settings")

    def test_rejects_non_release_build(self):
        self.cache.write_text(self.cache.read_text().replace("Release", "Debug"))
        self.run_guard("snapshot", "requires the production Release build")

    def test_rejects_unrelated_source_tree(self):
        self.cache.write_text(self.cache.read_text().replace(str(self.source), "/other"))
        self.run_guard("snapshot", "different source tree")

    def test_rejects_empty_production_build(self):
        self.obj.unlink()
        self.run_guard("snapshot", "No production objects available")


if __name__ == "__main__":
    unittest.main()
