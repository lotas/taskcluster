#!/usr/bin/env bash
# Task 13: write the NC12/NC15 fixture branch into a qf-research checkout.
#
# This is tooling, not a test. It writes files and prints the git commands; it
# never commits and never pushes, because the branch is the operator's to
# publish with the AGENT's credential (the dispatcher's token is read-only and
# must stay that way).
#
# Run it from anywhere:
#
#   ./nc-fixtures-phase2.sh ~/qf-research
#
# Then commit, push, and record the resulting SHA in host/nc12-sha.txt. The
# suite reads that file; a missing one is VOID, not skip, so an unpublished
# fixture branch is reported rather than quietly dropping NC12 and NC15.
#
# WHY SIX FIXTURES AND NOT THE PLAN'S FIVE. NC15's canary asserts that a
# well-behaved job's artifact lands at 0640 qfd:qfclient. An ordinary pytest run
# writes nothing to /out and the 2a allowlist is `result.json` alone, so without
# a fixture that WRITES one the canary voids on a working handoff -- and every
# refusal it guards then proves nothing. `artifact_good.py` is that canary.
#
# WHY THE ARTIFACT IS ALWAYS `result.json`. That is the entire 2a allowlist
# (`Runner._artifact_allowlist`); 2b widens it with typed contracts. The plan's
# NC15 prose says `predictions.parquet`, which the handoff would ignore -- a
# fixture writing it would produce no artifact at all and read as a handoff
# failure.
set -euo pipefail

REPO="${1:-}"
if [ -z "$REPO" ] || [ ! -d "$REPO/.git" ]; then
  echo "usage: $0 <path to a qf-research checkout>" >&2
  exit 2
fi
if [ ! -f "$REPO/trainer/pyproject.toml" ]; then
  echo "$REPO does not look like qf-research: no trainer/pyproject.toml" >&2
  exit 2
fi

EXP="$REPO/research/experiments"
mkdir -p "$EXP"

# Every fixture is a pytest MODULE with a `test_*` function, because the suite
# runs them through the ordinary `test` path (`--path research/experiments/x.py`).
# pytest collects an explicitly named file even when it does not match
# `python_files`, but the function inside must still match `python_functions` --
# so a bare script would collect nothing and exit 5, which now reports as
# `no_tests_collected` rather than as containment.

cat > "$EXP/artifact_good.py" <<'EOF'
"""NC15 canary: the well-behaved job.

Writes the one artifact the 2a allowlist names, so the suite can assert the
pre-create / --group-add / chmod sequence produced a 0640 qfd:qfclient file that
`research` can read. Every refusal in NC15 is measured against this: without a
job that produces an artifact, "no artifact appeared" is not evidence of
containment.
"""
import json


def test_writes_an_allowlisted_artifact():
    with open("/out/result.json", "w") as fh:
        json.dump({"fixture": "artifact_good", "ok": True}, fh)
EOF

cat > "$EXP/log_flood.py" <<'EOF'
"""NC15: an endless stream to stdout.

Expected: killed with error_class=log_overflow, and neither log file exceeds
QFD_LOG_CAP_MB. Written in 1 MiB chunks rather than per-line so the cap is
reached in seconds -- the point is the bound, not how long it takes to hit it.
"""
import sys


def test_floods_stdout():
    chunk = "x" * (1 << 20)
    while True:
        sys.stdout.write(chunk)
        sys.stdout.flush()
EOF

cat > "$EXP/disk_flood.py" <<'EOF'
"""NC15: endless writes into /out.

Expected: killed at OUT_QUOTA by the sampling watcher, with `du` of the run
directory confirming the bound held. Real bytes, not a sparse file: a hole
would test the quota against nothing.
"""


def test_floods_the_output_directory():
    block = b"\0" * (1 << 20)
    with open("/out/nc15-flood.bin", "wb") as fh:
        while True:
            fh.write(block)
            fh.flush()
EOF

cat > "$EXP/artifact_mode_0600.py" <<'EOF'
"""NC15: a hostile artifact MODE.

0600 owned by the candidate's uid is the case qfd cannot handle alone -- it owns
the artifacts directory but not this file, so it can neither read it nor chmod
it. The handoff runs as the candidate's own uid for exactly this reason
(design D9), so the run must still SUCCEED with a readable 0640 copy.
"""
import json
import os


def test_leaves_an_unreadable_artifact():
    path = "/out/result.json"
    with open(path, "w") as fh:
        json.dump({"fixture": "artifact_mode_0600"}, fh)
    os.chmod(path, 0o600)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
EOF

cat > "$EXP/artifact_symlink.py" <<'EOF'
"""NC15: a symlink at an allowlisted artifact name.

Pointing OUTSIDE the mount on purpose: following it would read a host file
through a name the candidate chose. The handoff tests -h before -f, because
`test -f` follows symlinks and would accept this one.

Expected: FAILED with error_class=handoff_bad_type, and nothing copied.
"""
import os


def test_leaves_a_symlink_where_an_artifact_belongs():
    os.symlink("/etc/passwd", "/out/result.json")
    assert os.path.islink("/out/result.json")
EOF

cat > "$EXP/artifact_fifo.py" <<'EOF'
"""NC15: a FIFO at an allowlisted artifact name.

The wedge case. Both `wc -c` and `cat` on a FIFO block for ever, so if the
handoff's file-type guard were removed this would hang until
QFD_HANDOFF_TIMEOUT_S -- which is why that timeout exists as a backstop. The
suite asserts the class AND the elapsed time, so it can tell which of the two
actually did the refusing.

Expected: FAILED with error_class=handoff_bad_type, well inside the timeout.
"""
import os
import stat


def test_leaves_a_fifo_where_an_artifact_belongs():
    os.mkfifo("/out/result.json")
    assert stat.S_ISFIFO(os.stat("/out/result.json").st_mode)
EOF

echo "wrote 6 fixtures to research/experiments/"

# --- the poisoned manifest (NC12) -----------------------------------------
MANIFEST="$REPO/trainer/pyproject.toml"
# OUR marker first. Checked before the [build-system] guard below, because on a
# second run the table this script itself added would trip that guard and the
# script would exit 3 on a checkout it had already prepared correctly.
if grep -q 'qf-nc12-bogus-backend' "$MANIFEST"; then
  echo "trainer/pyproject.toml is already poisoned; leaving it alone"
elif grep -q '^\[build-system\]' "$MANIFEST"; then
  echo
  echo "NOT touching $MANIFEST: it already has a [build-system] table, and" >&2
  echo "appending a second one produces invalid TOML -- which pytest would fail" >&2
  echo "to parse, turning NC12 into a broken job rather than a controlled one." >&2
  echo "Edit that table by hand instead: point build-backend at a backend that" >&2
  echo "does not exist and add a requirement that does not exist." >&2
  exit 3
else
  cat >> "$MANIFEST" <<'EOF'

# --- NC12 FIXTURE -- DO NOT "FIX" THIS -------------------------------------
# This table is deliberately unusable. NC12 asserts that the trainer image is
# built from the PROMOTED manifests in the dispatcher's own env/ directory and
# never from this file, so poisoning it must change nothing: the image content
# key stays byte-identical and neither package below reaches the image.
#
# It fails LOUDLY rather than silently on purpose. A backend that merely
# resolved to something harmless would let a real regression -- the research
# manifest being used after all -- pass unnoticed.
#
# pytest only reads [tool.pytest.ini_options] and never invokes a build backend,
# so jobs on this branch still run. `pip install -e trainer/` will not, which is
# the intended blast radius.
[build-system]
requires = [
  "qf-nc12-bogus-backend==0.0.0",
  "this-package-does-not-exist==9.9.9",
]
build-backend = "qf_nc12_bogus_backend"
EOF
  echo "poisoned trainer/pyproject.toml"
fi

cat <<'NEXT'

Next, in the qf-research checkout, with the AGENT's credential:

  git checkout -b nc12-poisoned-manifest
  git add research/experiments trainer/pyproject.toml
  git commit -m 'NC12/NC15 fixtures: poisoned manifest and six hostile jobs'
  git push -u origin nc12-poisoned-manifest
  git rev-parse HEAD

Then, in THIS repo:

  echo <that sha> > tools/queue-forecasting/host/nc12-sha.txt

and commit it, so later suite runs are automatic rather than VOID. Leave the
branch in place: it is a fixture, not litter.
NEXT
