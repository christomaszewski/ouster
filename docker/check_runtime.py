"""Check the compiled overlay against its final runtime (no sensor or ROS node needed)."""

import argparse
from pathlib import Path
import subprocess
import sys


def check_runtime(prefix, build_packages=None):
    errors = []
    if build_packages is not None:
        built = dict(line.split("\t") for line in build_packages.read_text().splitlines())
        packages = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n", "ros-*"],
            text=True,
        )
        for line in packages.splitlines():
            name, version, status = line.split("\t")
            if status == "installed" and name in built and built[name] != version:
                errors.append(f"{name}: build={built[name]}, runtime={version}")

    # ROS libraries live directly in lib/. Python extensions under site-packages depend
    # on interpreter-provided symbols and are exercised by the packaged Python tests.
    libraries = sorted((prefix / "lib").glob("lib*.so"))
    if not libraries:
        errors.append(f"no ROS shared libraries found in {prefix / 'lib'}")
    for library in libraries:
        # ctypes always adds RTLD_NOW. Isolate each load so plugin registrations and
        # previously loaded libraries cannot hide a failure in another library.
        result = subprocess.run(
            [sys.executable, "-c", "import ctypes, sys; ctypes.CDLL(sys.argv[1])", str(library)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            errors.append(f"{library}: exit {result.returncode}\n{result.stderr.strip()}")
    if errors:
        print("ERROR: Ouster build/runtime ABI check failed:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Ouster runtime ABI check passed ({len(libraries)} shared libraries)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--build-packages", type=Path)
    args = parser.parse_args()
    sys.exit(check_runtime(args.prefix, args.build_packages))
