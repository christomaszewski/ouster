# Common tasks for the ouster wrapper. Install `just`: https://github.com/casey/just
#   brew install just     (or: cargo install just)
# Run `just` with no args to list recipes.

# list available recipes
default:
    @just --list

# vcs import the pinned upstream ouster-ros into src/ (gitignored). Needs vcstool on the host
# (apt install python3-vcstool). The Docker build does this itself — `just vendor` is for local dev.
vendor:
    mkdir -p src
    vcs import src < ouster.repos
    git -C src/ouster-ros submodule update --init --recursive 2>/dev/null || true

# colcon build the vendored workspace (Release). Run `just vendor` first; needs a ROS 2 lyrical env.
# First time also: rosdep install --from-paths src --ignore-src -y
build:
    colcon build --base-paths src --merge-install \
      --cmake-args -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-Wno-deprecated-declarations"

# build the self-contained runtime image (fetches upstream itself; no `just vendor` needed)
image tag="latest":
    docker build -f docker/Dockerfile.runtime -t ouster_driver:{{tag}} .

# run against a live sensor (copy + edit sensors/ouster.example.yaml first)
run config:
    ./ouster-up {{config}} up

# replay a recorded rosbag2 of raw packets + metadata through rviz (see docker/compose/compose.replay.yaml)
replay bag metadata:
    OUSTER_REPLAY_BAG={{bag}} OUSTER_REPLAY_METADATA={{metadata}} \
      docker compose -f docker/compose/compose.replay.yaml up

# launcher-contract gate (same check CI / rig run)
certify:
    ../bringup/rig certify --repo . --config sensors/ouster.example.yaml

# start the dev container and open a shell in it
dev:
    docker compose -f docker/compose/compose.dev.yaml up -d
    docker compose -f docker/compose/compose.dev.yaml exec dev bash
