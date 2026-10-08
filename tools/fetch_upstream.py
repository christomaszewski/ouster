#!/usr/bin/env python3
"""Fetch the pinned ROS driver and SDK override into a fresh source directory."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def fetch(source_root, manifest_root):
    source_root = Path(source_root).resolve()
    manifest_root = Path(manifest_root).resolve()
    destination = source_root / "ouster-ros"
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing to replace {destination}; use a fresh source directory")

    manifests = {}
    for name in ("ouster", "ouster-sdk"):
        path = manifest_root / f"{name}.repos"
        repositories = yaml.safe_load(path.read_text())["repositories"]
        key = "ouster-ros" if name == "ouster" else name
        if set(repositories) != {key} or repositories[key]["type"] != "git":
            raise ValueError(f"{path} must contain just the {key} Git repository")
        manifests[key] = (path, repositories[key])
    sdk_ref = str(manifests["ouster-sdk"][1]["version"])
    if not re.fullmatch(r"[0-9a-f]{40}", sdk_ref):
        raise ValueError("the SDK override must be pinned to a full commit SHA")

    source_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ouster-fetch-", dir=source_root) as tmp:
        staging = Path(tmp)
        sources = {}
        for key, (manifest, spec) in manifests.items():
            with manifest.open() as stream:
                run("vcs", "import", str(staging), stdin=stream)
            checkout = staging / key
            run("git", "-C", str(checkout), "submodule", "update", "--init", "--recursive")
            revision = run("git", "-C", str(checkout), "rev-parse", "HEAD",
                           capture_output=True).stdout.strip()
            ref = str(spec["version"])
            if re.fullmatch(r"[0-9a-f]{40}", ref) and revision != ref:
                raise ValueError(f"{key}: fetched {revision}, expected {ref}")
            sources[key] = {"repo": spec["url"], "ref": ref, "rev": revision}

        ros = staging / "ouster-ros"
        sdk_destination = ros / "ouster-ros" / "ouster-sdk"
        if not (ros / "ouster-ros" / "package.xml").is_file():
            raise ValueError("unexpected ouster-ros source layout")
        if sdk_destination.is_symlink():
            raise ValueError("refusing a symlink at the SDK destination")
        # This only removes files from the fresh temporary checkout, never an
        # existing developer checkout. Supports both vendored and submodule SDKs.
        if sdk_destination.exists():
            shutil.rmtree(sdk_destination)
        (staging / "ouster-sdk").rename(sdk_destination)
        (ros / "upstream-sources.json").write_text(
            json.dumps({"version": 1, "sources": sources}, indent=2) + "\n")
        ros.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", nargs="?", default="src")
    parser.add_argument("--manifest-root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        print(fetch(args.source_root, args.manifest_root))
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"fetch_upstream: {error}\n")


if __name__ == "__main__":
    main()
