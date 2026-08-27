#!/bin/sh
# provenance-record.sh — the copyable service-side half of the fleet-msgs provenance convention.
#
# Appends one interface-repo entry to a msgs provenance file (creating the file with its header
# when absent), recording the symbolic ref a checkout used AND the commit SHA it resolved to.
# `rig image audit` (>= v0.2.30) compares this against the rigging's `msgs.source` declaration and
# the overlay's own provenance to catch silent pin skew. Schema + doctrine:
# provenance.example.yaml (sibling file).
#
#   provenance-record.sh FILE REPO REF CHECKOUT_DIR [PACKAGE...]
#
#   FILE          provenance file to append to — bake it at /opt/fleet-msgs/provenance.yaml, the
#                 ONE well-known path audit probes on every image.
#   REPO          the repo URL as the rigging's `msgs.source.repo` declares it (the audit join
#                 key). If the build actually cloned somewhere else (a mirror), still put the
#                 declared upstream here and record the mirror separately (see the example file).
#   REF           the symbolic ref THE CHECKOUT ACTUALLY USED — pass the very variable that drove
#                 `git checkout`, never a re-typed copy of the rigging value: a re-echoed
#                 declaration makes the audit circular (it would compare the declaration against
#                 itself, and real drift stays invisible).
#   CHECKOUT_DIR  the working tree that was checked out; its `git rev-parse HEAD` becomes `rev`
#                 (content identity — catches moved tags and re-built branches that symbolic refs
#                 cannot). A non-git dir (vendored snapshot) records `rev: unknown` explicitly:
#                 audit then WARNs "unverifiable" instead of staying silent — explicit unknown
#                 proves adoption and marks the gap, absence looks like non-adoption.
#   PACKAGE...    optional: the interface packages built from this repo.
#
# Use it in the SAME build stage as the checkout, immediately after it. Interface repos are
# typically built in a throwaway stage — the DEFAULT FAILURE MODE is provenance dying with that
# stage, so a multi-stage Dockerfile MUST copy the file into the final image:
#
#   COPY provenance-record.sh /usr/local/bin/provenance-record.sh   # vendored copy of this file
#   ARG PX4_MSGS_REF=v1.16.0
#   RUN git clone https://github.com/PX4/px4_msgs.git /ws/src/px4_msgs \
#    && git -C /ws/src/px4_msgs checkout --detach "${PX4_MSGS_REF}" \
#    && sh /usr/local/bin/provenance-record.sh /opt/fleet-msgs/provenance.yaml \
#         https://github.com/PX4/px4_msgs.git "${PX4_MSGS_REF}" /ws/src/px4_msgs px4_msgs
#   ...
#   FROM ${BASE_IMAGE}                                              # final stage
#   COPY --from=builder /opt/fleet-msgs/provenance.yaml /opt/fleet-msgs/provenance.yaml
#
# Plain POSIX sh + printf — no yaml tooling mid-stage. Values are written as plain YAML scalars,
# which every real repo URL, tag, branch, SHA, and ROS package name is; quote by hand if you
# genuinely have a ref with YAML-special characters.
set -eu
usage="usage: provenance-record.sh FILE REPO REF CHECKOUT_DIR [PACKAGE...]"
file="${1:?$usage}"
repo="${2:?$usage}"
ref="${3:?$usage}"
dir="${4:?$usage}"
shift 4

rev="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo unknown)"

if [ ! -f "$file" ]; then
    mkdir -p "$(dirname "$file")"
    printf 'version: 1\nsource:\n' > "$file"
fi
{
    printf '  - repo: %s\n    ref: %s\n    rev: %s\n' "$repo" "$ref" "$rev"
    if [ "$#" -gt 0 ]; then
        pkgs=""
        for p in "$@"; do pkgs="${pkgs:+$pkgs, }$p"; done
        printf '    packages: [%s]\n' "$pkgs"
    fi
} >> "$file"
