"""Properties of docker/Dockerfile.runtime that a successful build would not report."""

from itertools import takewhile
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeDockerfileTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "docker/Dockerfile.runtime").read_text()
        self.stages = {}
        name = None
        for line in self.text.splitlines():
            start = re.match(r"FROM\s+\S+\s+AS\s+(\S+)", line)
            if start:
                name = start.group(1)
            if name:
                self.stages.setdefault(name, []).append(line)

    def stage(self, name):
        return "\n".join(self.stages[name])

    def test_frontend_is_the_builders_own(self):
        # Parser directives only count in the leading comment block. A `# syntax=` there makes
        # every build, cached or not, resolve docker/dockerfile on Docker Hub first: a rate limit
        # or a disconnected builder (`rig build --offline`) then fails before any step runs.
        leading = takewhile(lambda line: line.startswith("#"), self.text.splitlines())
        self.assertFalse([line for line in leading if re.match(r"#\s*syntax\s*=", line)])

    def test_builder_caches_stay_out_of_the_shipped_stage(self):
        runtime = self.stage("runtime")
        self.assertNotIn("--mount=type=cache", runtime)
        # The apt download cache needs the parent's cleanup hook replaced; only build stages do it.
        self.assertNotIn("docker-clean", "\n".join(
            line for line in self.stages["runtime"] if not line.lstrip().startswith("#")))
        for name in ("toolchain", "build-deps", "build"):
            self.assertIn("--mount=type=cache", self.stage(name), name)

    def test_cache_mounts_are_per_distro_and_architecture_with_one_writer(self):
        mounts = re.findall(r"--mount=type=cache,(\S+)", self.text)
        self.assertGreaterEqual(len(mounts), 4)
        for mount in mounts:
            options = dict(option.split("=", 1) for option in mount.split(","))
            self.assertRegex(options["id"], r"^ouster-(apt|ccache)-\$\{ROS_DISTRO\}-\$\{TARGETARCH\}$")
            self.assertEqual(options["sharing"], "locked")
        # The id uses TARGETARCH, which a stage only sees once it (or its parent stage) declares it.
        self.assertIn("ARG TARGETARCH", self.stage("toolchain"))

    def test_compiler_cache_is_bypassed_only_on_request(self):
        self.assertIn("ARG OUSTER_DISABLE_CCACHE=0", self.stage("build"))


if __name__ == "__main__":
    unittest.main()
