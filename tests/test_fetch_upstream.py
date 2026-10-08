"""Exercise source replacement and provenance with local Git repositories."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_upstream", ROOT / "tools/fetch_upstream.py")
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


class FetchUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "src"
        self.manifests = self.root / "manifests"
        self.manifests.mkdir()

    def repository(self, name, files):
        repo = self.root / name
        repo.mkdir()
        for name, contents in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args],
                                           text=True, stderr=subprocess.STDOUT).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Fixture")
        return repo, git("rev-parse", "HEAD")

    def manifest(self, filename, key, repo, ref):
        (self.manifests / filename).write_text(yaml.safe_dump({"repositories": {
            key: {"type": "git", "url": str(repo), "version": ref}}}))

    def test_existing_checkout_is_never_replaced(self):
        checkout = self.output / "ouster-ros"
        checkout.mkdir(parents=True)
        marker = checkout / "local-work"
        marker.write_text("keep me")
        with self.assertRaisesRegex(ValueError, "refusing to replace"):
            fetcher.fetch(self.output, self.manifests)
        self.assertEqual(marker.read_text(), "keep me")

    def test_sdk_requires_immutable_commit(self):
        self.manifest("ouster.repos", "ouster-ros", "unused", "0.15.1")
        self.manifest("ouster-sdk.repos", "ouster-sdk", "unused", "main")
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            fetcher.fetch(self.output, self.manifests)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which("vcs"), "requires vcstool (available in build image)")
    def test_override_and_provenance_use_actual_checkouts(self):
        ros, ros_rev = self.repository("ros-source", {
            "ouster-ros/package.xml": "<package/>",
            "ouster-ros/ouster-sdk/old": "old bundled SDK"})
        sdk, sdk_rev = self.repository("sdk-source", {"VERSION": "patched SDK"})
        self.manifest("ouster.repos", "ouster-ros", ros, ros_rev)
        self.manifest("ouster-sdk.repos", "ouster-sdk", sdk, sdk_rev)
        checkout = fetcher.fetch(self.output, self.manifests)
        installed_sdk = checkout / "ouster-ros/ouster-sdk"
        self.assertFalse((installed_sdk / "old").exists())
        self.assertEqual((installed_sdk / "VERSION").read_text(), "patched SDK")
        self.assertEqual(subprocess.check_output(
            ["git", "-C", str(installed_sdk), "rev-parse", "HEAD"], text=True).strip(), sdk_rev)
        sources = json.loads((checkout / "upstream-sources.json").read_text())["sources"]
        self.assertEqual(sources["ouster-ros"], {"repo": str(ros), "ref": ros_rev, "rev": ros_rev})
        self.assertEqual(sources["ouster-sdk"], {"repo": str(sdk), "ref": sdk_rev, "rev": sdk_rev})
        self.assertFalse(list(self.output.glob(".ouster-fetch-*")))


if __name__ == "__main__":
    unittest.main()
