#!/usr/bin/env bash
# Run one experiment as the identity that owns it.
#
# WHY A WRAPPER. `experiment.py` deliberately does not elevate: it commits into
# a checkout, pushes with a credential and submits jobs, and a tool that can
# switch identity is a tool an agent must not be handed. So the elevation lives
# here, in the operator's copy, and the agent runs the python directly as
# itself.
#
# WHY IT RUNS THE /srv COPY AND NOT THE ONE BESIDE IT. Two reasons, and the
# second is the real one:
#
#   1. The research user cannot READ an operator's home. `~/dev/...` is not
#      traversable by another account, so `sudo -u research python3 $HERE/...`
#      fails with EACCES on a file whose own mode is 0755.
#   2. The research identity must not execute an operator's working copy. `qfd`
#      runs from the trusted mirror for exactly this reason: what another
#      privilege domain executes is deployed code, reviewed and pushed, not
#      whatever is currently saved in an editor.
#
# So an edit here does nothing until `phase2-setup.sh mirror-refresh` lands it
# in /srv -- and because that has been a real trap before (a scoreboard fix sat
# undeployed while its verdicts looked simply wrong), this SAYS SO when the two
# copies differ rather than silently running the old one.
#
#   ./host/experiment.sh doctor
#   ./host/experiment.sh plan configs/wait_hazard_qctx_d_priority_flow.yaml
#   ./host/experiment.sh run  configs/wait_hazard_qctx_d_priority_flow.yaml \
#       --note "hazard on qctx_d features"
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED_HOST="${QF_TRUSTED_HOST:-/srv/queue-forecasting/tools/queue-forecasting/host}"
SCRIPT="$TRUSTED_HOST/experiment.py"

# The same array-not-wrapper shape `results.sh` uses, for the same reason: a
# `run()` function with an empty prefix calls itself.
AS=(sudo -H -u research)
if [ "$(id -un)" = "research" ]; then
  AS=()
  # Already the research user, and reachable code is reachable code: if this
  # script IS the trusted copy, there is nothing to compare or elevate.
  [ "$HERE" != "$TRUSTED_HOST" ] || SCRIPT="$HERE/experiment.py"
fi

# TESTED AS THE IDENTITY THAT WILL RUN IT, not as the caller. A file the
# operator can read and the research user cannot is the exact failure this
# whole block exists for, and `[ -r ]` here would report it as fine.
if ! "${AS[@]}" test -r "$SCRIPT"; then
  echo "cannot read $SCRIPT" >&2
  echo "  The research user runs the TRUSTED copy, not this checkout." >&2
  echo "  Deploy it first:" >&2
  echo "    sudo TRUSTED_REF=<your branch> $HERE/phase2-setup.sh mirror-refresh" >&2
  echo "  Or point this at another readable copy with QF_TRUSTED_HOST=<dir>." >&2
  exit 1
fi

# NOT a hard failure: running the deployed copy is correct, and an operator
# mid-edit does not need to be blocked. But saying nothing is how an edit gets
# tested for an hour without ever having run.
if [ "$HERE" != "$TRUSTED_HOST" ] && [ -r "$HERE/experiment.py" ] \
   && ! cmp -s "$HERE/experiment.py" "$SCRIPT"; then
  echo "note: running the DEPLOYED $SCRIPT," >&2
  echo "      which differs from $HERE/experiment.py." >&2
  echo "      Your edits are not in this run until mirror-refresh." >&2
fi

exec "${AS[@]}" python3 "$SCRIPT" "$@"
