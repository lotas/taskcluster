#!/usr/bin/env bash
# Run one experiment as the identity that owns it.
#
# WHY A WRAPPER. `experiment.py` deliberately does not elevate: it commits into
# a checkout, pushes with a credential and submits jobs, and a tool that can
# switch identity is a tool an agent must not be handed. So the elevation lives
# here, in the operator's copy, and the agent runs the python directly as
# itself.
#
#   ./host/experiment.sh doctor
#   ./host/experiment.sh plan configs/wait_hazard_qctx_d_priority_flow.yaml
#   ./host/experiment.sh run  configs/wait_hazard_qctx_d_priority_flow.yaml \
#       --note "hazard on qctx_d features"
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The same array-not-wrapper shape `results.sh` uses, for the same reason: a
# `run()` function with an empty prefix calls itself.
AS=(sudo -H -u research)
[ "$(id -un)" != "research" ] || AS=()

exec "${AS[@]}" python3 "$HERE/experiment.py" "$@"
