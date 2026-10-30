#!/usr/bin/env python3
"""Fetch the pinned ROS driver and SDK override into a fresh source directory.

Two ways to obtain the pinned trees, one result:

  clone   (standalone builds) `vcs import` each .repos manifest, then initialize submodules.
  export  (under rig >= v0.2.74) rig exports each `build.sources` entry from its local git mirror
          and tools/build_image.sh hands the trees over as BuildKit named contexts, which the
          Dockerfile mounts under --exports. Nothing is fetched, so `rig build --offline` works.
          build_image.sh has already checked that rig resolved each declared ref to exactly the
          pinned commit, so the pin is the revision recorded here.

Either way the bundled SDK is replaced by the override and upstream-sources.json records each
repository, its requested ref and the commit actually built.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

SOURCES = ("ouster-ros", "ouster-sdk")
SHA = re.compile(r"[0-9a-f]{40}")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def load_manifests(manifest_root):
    manifests = {}
    for name in ("ouster", "ouster-sdk"):
        path = manifest_root / f"{name}.repos"
        repositories = yaml.safe_load(path.read_text())["repositories"]
        key = "ouster-ros" if name == "ouster" else name
        if set(repositories) != {key} or repositories[key]["type"] != "git":
            raise ValueError(f"{path} must contain just the {key} Git repository")
        manifests[key] = (path, repositories[key])
    return manifests


def export_trees(exports):
    """The exported tree for each source, {} when none were exported (a standalone build binds
    the Dockerfile's empty default stages). A partial export is refused: half a rig build would
    otherwise silently clone the other half."""
    if exports is None:
        return {}
    trees = {key: Path(exports) / key for key in SOURCES}
    present = {key for key, tree in trees.items() if tree.is_dir() and any(tree.iterdir())}
    if not present:
        return {}
    if present != set(SOURCES):
        missing = ", ".join(sorted(set(SOURCES) - present))
        raise ValueError(f"exported sources are incomplete (missing {missing}); "
                         f"declare every one in rigging.yaml build.sources")
    return trees


def fetch(source_root, manifest_root, exports=None):
    source_root = Path(source_root).resolve()
    manifest_root = Path(manifest_root).resolve()
    destination = source_root / "ouster-ros"
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing to replace {destination}; use a fresh source directory")

    manifests = load_manifests(manifest_root)
    sdk_ref = str(manifests["ouster-sdk"][1]["version"])
    if not SHA.fullmatch(sdk_ref):
        raise ValueError("the SDK override must be pinned to a full commit SHA")
    trees = export_trees(exports)
    if trees:
        for key, (path, spec) in manifests.items():
            if not SHA.fullmatch(str(spec["version"])):
                raise ValueError(f"{path}: exported sources need a full commit SHA pin")

    source_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ouster-fetch-", dir=source_root) as tmp:
        staging = Path(tmp)
        sources = {}
        for key, (manifest, spec) in manifests.items():
            checkout = staging / key
            ref = str(spec["version"])
            if trees:
                # rig's export: the tree at the pinned commit, submodules included, no .git.
                shutil.copytree(trees[key], checkout, symlinks=True)
                revision = ref
            else:
                with manifest.open() as stream:
                    run("vcs", "import", str(staging), stdin=stream)
                # The bundled SDK is replaced below. Do not fetch it (or its
                # children), but still initialize every other required submodule.
                # This also handles revisions where the SDK is a vendored tree.
                paths = ("--", ".", ":(exclude)ouster-ros/ouster-sdk") if key == "ouster-ros" else ()
                run("git", "-C", str(checkout), "submodule", "update", "--init", "--recursive", *paths)
                revision = run("git", "-C", str(checkout), "rev-parse", "HEAD",
                               capture_output=True).stdout.strip()
                if SHA.fullmatch(ref) and revision != ref:
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


def write_msgs_provenance(checkout, output, packages):
    """Record the interface repo this checkout built in the fleet-msgs provenance file that
    `rig image audit` (>= v0.2.30) probes: the declared repo and ref plus the commit built, taken
    from upstream-sources.json (the .repos pin, never re-typed from rigging.yaml, so the audit
    compares the declaration against what was actually built). This is the schema rig-infra's
    msgs/provenance-record.sh writes; that helper reads the commit from git, which an exported
    tree does not carry."""
    source = json.loads((Path(checkout) / "upstream-sources.json").read_text())["sources"]["ouster-ros"]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    entry = {"repo": source["repo"], "ref": source["ref"], "rev": source["rev"],
             "packages": list(packages)}
    output.write_text(yaml.safe_dump({"version": 1, "source": [entry]}, sort_keys=False))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source_root", nargs="?", default="src")
    parser.add_argument("--manifest-root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--exports", type=Path,
                        help="directory holding rig's exported ouster-ros/ and ouster-sdk/ trees; "
                             "empty directories mean clone")
    parser.add_argument("--msgs-provenance", type=Path, metavar="FILE",
                        help="also write the fleet-msgs provenance file for ouster-ros")
    parser.add_argument("--msgs-package", action="append", default=[], metavar="PACKAGE",
                        help="interface package built from ouster-ros (repeatable)")
    args = parser.parse_args()
    if args.msgs_package and not args.msgs_provenance:
        parser.error("--msgs-package needs --msgs-provenance")
    try:
        checkout = fetch(args.source_root, args.manifest_root, args.exports)
        if args.msgs_provenance:
            write_msgs_provenance(checkout, args.msgs_provenance, args.msgs_package)
        print(checkout)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"fetch_upstream: {error}\n")


if __name__ == "__main__":
    main()
