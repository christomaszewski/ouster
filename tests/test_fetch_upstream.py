"""Exercise source replacement and provenance with local Git repositories."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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

    def test_patched_sources_are_immutable_and_messages_match_driver(self):
        driver = yaml.safe_load((ROOT / "ouster.repos").read_text())["repositories"]["ouster-ros"]
        sdk = yaml.safe_load((ROOT / "ouster-sdk.repos").read_text())["repositories"]["ouster-sdk"]
        for source in (driver, sdk):
            self.assertRegex(str(source["version"]), r"^[0-9a-f]{40}$")
        message_sources = yaml.safe_load((ROOT / "rigging.yaml").read_text())["msgs"]["source"]
        matches = [source for source in message_sources
                   if "ouster_sensor_msgs" in source["packages"]]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["repo"], driver["url"])
        self.assertEqual(str(matches[0]["ref"]), str(driver["version"]))

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

    def submodule(self, repo, name, path, target, revision):
        """Record a Gitlink without fetching its possibly unavailable remote."""
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args],
                                           text=True, stderr=subprocess.STDOUT).strip()
        git("config", "--file", ".gitmodules", f"submodule.{name}.path", path)
        git("config", "--file", ".gitmodules", f"submodule.{name}.url", str(target))
        git("add", ".gitmodules")
        git("update-index", "--add", "--cacheinfo", f"160000,{revision},{path}")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Add submodule fixture")
        return git("rev-parse", "HEAD")

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

    @unittest.skipUnless(shutil.which("vcs"), "requires vcstool (available in build image)")
    def test_unavailable_bundled_sdk_is_skipped_but_required_submodules_are_recursive(self):
        leaf, leaf_rev = self.repository("leaf-source", {"marker": "required nested dependency"})
        dependency, dependency_rev = self.repository("dependency-source", {"marker": "required dependency"})
        dependency_rev = self.submodule(dependency, "nested", "nested", leaf, leaf_rev)
        ros, ros_rev = self.repository("ros-source", {"ouster-ros/package.xml": "<package/>"})
        # The submodule name deliberately differs from its path: selection must
        # exclude the destination we replace, not a particular upstream name.
        ros_rev = self.submodule(ros, "unavailable-sdk", "ouster-ros/ouster-sdk",
                                 self.root / "unavailable-sdk", leaf_rev)
        ros_rev = self.submodule(ros, "required", "ouster-ros/required", dependency, dependency_rev)
        sdk, sdk_rev = self.repository("sdk-source", {"VERSION": "patched SDK"})
        sdk_rev = self.submodule(sdk, "required", "required", dependency, dependency_rev)
        self.manifest("ouster.repos", "ouster-ros", ros, ros_rev)
        self.manifest("ouster-sdk.repos", "ouster-sdk", sdk, sdk_rev)

        # Git disables the file transport for submodules by default. These
        # fixtures are all local and never need network access.
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            checkout = fetcher.fetch(self.output, self.manifests)
        installed_sdk = checkout / "ouster-ros/ouster-sdk"
        self.assertEqual((installed_sdk / "VERSION").read_text(), "patched SDK")
        for dependency_root in (checkout / "ouster-ros/required", installed_sdk / "required"):
            self.assertEqual((dependency_root / "marker").read_text(), "required dependency")
            self.assertEqual((dependency_root / "nested/marker").read_text(), "required nested dependency")
        sources = json.loads((checkout / "upstream-sources.json").read_text())["sources"]
        self.assertEqual(sources["ouster-ros"]["rev"], ros_rev)
        self.assertEqual(sources["ouster-sdk"]["rev"], sdk_rev)
        self.assertFalse(list(self.output.glob(".ouster-fetch-*")))

    @unittest.skipUnless(shutil.which("vcs"), "requires vcstool (available in build image)")
    def test_unavailable_required_submodules_fail_without_installing_partial_checkout(self):
        for owner in ("ros", "sdk"):
            with self.subTest(owner=owner):
                ros, ros_rev = self.repository(f"{owner}-ros-source", {
                    "ouster-ros/package.xml": "<package/>",
                    "ouster-ros/ouster-sdk/old": "old bundled SDK"})
                sdk, sdk_rev = self.repository(f"{owner}-sdk-source", {"VERSION": "patched SDK"})
                if owner == "ros":
                    ros_rev = self.submodule(ros, "required", "ouster-ros/required",
                                             self.root / "unavailable", sdk_rev)
                else:
                    sdk_rev = self.submodule(sdk, "required", "required",
                                             self.root / "unavailable", ros_rev)
                self.manifest("ouster.repos", "ouster-ros", ros, ros_rev)
                self.manifest("ouster-sdk.repos", "ouster-sdk", sdk, sdk_rev)
                with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
                    with self.assertRaises(subprocess.CalledProcessError):
                        fetcher.fetch(self.output, self.manifests)
                self.assertFalse((self.output / "ouster-ros").exists())
                self.assertFalse(list(self.output.glob(".ouster-fetch-*")))


if __name__ == "__main__":
    unittest.main()
