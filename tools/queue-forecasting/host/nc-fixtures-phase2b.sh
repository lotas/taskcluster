#!/usr/bin/env bash
# Phase 2b-2: write the probe fixture into a qf-research checkout.
#
# Tooling, not a test. It writes files and prints the git commands; it never
# commits and never pushes, because the branch is the operator's to publish with
# the AGENT's credential -- the dispatcher's token is read-only and must stay so.
#
#   ./nc-fixtures-phase2b.sh ~/qf-research
#
# WHAT THIS FIXTURE PROVES, AND WHAT IT DOES NOT.
#
# It proves everything 2b-2's code owns: that `/extract` arrives read-only with a
# readable manifest, that `/app/trainer/data` is the one writable hole in a
# read-only tree, that the sandbox still has no network and no credential, that
# every extract file is readable from inside, and that a `predictions.parquet`
# with the frozen columns is collected into `artifacts/`.
#
# It does NOT train a model, and that gap is deliberate rather than convenient.
# The plan's 2b-2 acceptance says "one cohort reproduces from a frozen extract",
# and reproducing a cohort needs three things this fixture is not:
#
#   1. a loader that builds the trainer's frame from parquet instead of from
#      `_build_query` -- `data_loader.py` reads `os.environ["DATABASE_URL"]` in
#      six places and raises KeyError before it starts;
#   2. the feature pipeline and the model, run against that frame;
#   3. a comparison against a recorded result, which is what "reproduces" means.
#
# All three are changes to the trainer's DATA PATH, not to the dispatcher's
# plumbing -- and a change to the trainer's data path should not be smuggled in as
# a test fixture. So this fixture closes the plumbing, and the cohort
# reproduction is a named next step against the real loader.
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

cat > "$EXP/extract_contract.py" <<'EOF'
"""2b-2: the probe's view of the data plane, asserted from inside the sandbox.

Run as a `probe`, so it is agent-authored code under `research/experiments/` --
which is exactly why the three "/extract is present, readable, not writable"
clauses live here and not in `nc13-inside.sh`. That script runs as a SELFTEST,
and a selftest requests no extract; it asserts the ABSENCE of one, which is the
other half of the same control: the data plane must not be ambient.

Prints an `== EXTRACT-CONTRACT: pass=N fail=N ==` summary, which the NC suite
greps for the same way it greps NC13's.
"""
import json
import os
import socket
import sys

EXTRACT = "/extract"
# TWO LEVELS DOWN, and the first version of this fixture said one.
#
# `extract-qf-research.sh` renames `tools/queue-forecasting/trainer/` to
# `trainer/`, and the dispatcher mounts the WORKTREE ROOT at `/app/trainer` --
# which is also why a `test` job's default path is `trainer/tests`. `CACHE_DIR`
# is `<module>/../data`, so it lands here. Mounting one level short made the
# container refuse to start with a read-only-filesystem error about a mountpoint,
# naming neither the wrong path nor the missing directory.
DATA = "/app/trainer/trainer/data"
OUT = "/out"

_pass = 0
_fail = 0


def ok(msg):
    global _pass
    _pass += 1
    print(f"ok    {msg}", flush=True)


def bad(msg):
    global _fail
    _fail += 1
    print(f"FAIL  {msg}", flush=True)


def check(condition, good, ill):
    ok(good) if condition else bad(ill)


def main():
    # --- the extract is there, and it is READ-ONLY ------------------------
    check(os.path.isdir(EXTRACT),
          f"{EXTRACT} is mounted",
          f"{EXTRACT} is missing: a probe without its extract has no data")

    manifest = None
    path = os.path.join(EXTRACT, "MANIFEST.json")
    try:
        with open(path) as fh:
            manifest = json.load(fh)
        ok("MANIFEST.json is readable and is JSON")
    except Exception as e:                                     # noqa: BLE001
        bad(f"MANIFEST.json is unreadable: {e}")

    # THE ASSERTION THAT MATTERS. A published extract is immutable (D20), and a
    # run that could write to it would change the input to results that already
    # cite it -- invisibly, because the manifest's digests describe what was
    # extracted and nothing re-checks them before a later read.
    try:
        with open(os.path.join(EXTRACT, ".probe-write"), "w") as fh:
            fh.write("x")
        bad(f"{EXTRACT} is WRITABLE: an immutable artifact can be rewritten")
        os.unlink(os.path.join(EXTRACT, ".probe-write"))
    except OSError:
        ok(f"{EXTRACT} is not writable")

    # ... and so is the tree around it.
    try:
        with open("/app/trainer/.probe-write", "w") as fh:
            fh.write("x")
        bad("/app/trainer is writable: the source mount lost its :ro")
    except OSError:
        ok("/app/trainer is not writable")

    # --- the ONE writable hole -------------------------------------------
    # `CACHE_DIR` is computed relative to the trainer module, so this has to work
    # even though everything around it is read-only. A read-only mount here fails
    # deep inside pandas with an error naming a path nobody chose.
    try:
        os.makedirs(os.path.join(DATA, "cache"), exist_ok=True)
        with open(os.path.join(DATA, "cache", ".probe-write"), "w") as fh:
            fh.write("x")
        ok(f"{DATA} is writable, so CACHE_DIR works without a path refactor")
    except OSError as e:
        bad(f"{DATA} is not writable ({e}): the trainer cannot cache")

    # --- still no credential, still no network ---------------------------
    check(not os.environ.get("DATABASE_URL"),
          "DATABASE_URL is unset",
          "DATABASE_URL is SET: the candidate has a credential")
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        bad("outbound network works: --network none is not in force")
    except OSError:
        ok("no outbound network")

    # --- every file the manifest names is readable -----------------------
    if manifest:
        files = manifest.get("files") or {}
        if not files:
            bad("the manifest lists no files")
        for name, entry in sorted(files.items()):
            target = os.path.join(EXTRACT, entry.get("file", name + ".parquet"))
            try:
                size = os.path.getsize(target)
            except OSError as e:
                bad(f"{name}: unreadable ({e})")
                continue
            # SIZE AND ROWS, both: a file that exists and is empty would pass a
            # bare existence check while carrying nothing.
            if size > 0 and entry.get("rows"):
                ok(f"{name}: {entry['rows']} rows, {size} bytes")
            else:
                bad(f"{name}: size={size} rows={entry.get('rows')}")

        # The provenance a later reader needs, echoed so it is in the run's log
        # as well as the manifest.
        print(f"    extract_hash={manifest.get('extract_hash', '?')[:12]}"
              f" watermark={manifest.get('watermark')}", flush=True)

    # --- and the predictions contract ------------------------------------
    # Written from REAL keys out of the extract, so the file has the frozen
    # shape design section 4.6 specifies rather than a plausible-looking stub.
    # `p50`/`p90_raw` are placeholders: this fixture proves the CONTRACT and the
    # collection path, not a model.
    try:
        import pyarrow            # noqa: PLC0415
        import pyarrow.parquet    # noqa: PLC0415

        runs = pyarrow.parquet.read_table(
            os.path.join(EXTRACT, "runs.parquet"),
            columns=["task_id", "run_id"]).slice(0, 1000)
        task_id = [str(v) for v in runs.column("task_id").to_pylist()]
        run_id = [int(v) for v in runs.column("run_id").to_pylist()]
        table = pyarrow.table({
            "task_id": pyarrow.array(task_id, pyarrow.string()),
            "run_id": pyarrow.array(run_id, pyarrow.int32()),
            "row_id": pyarrow.array([f"{t}:{r}" for t, r in
                                     zip(task_id, run_id)], pyarrow.string()),
            "p50": pyarrow.array([1.0] * len(task_id), pyarrow.float64()),
            "p90_raw": pyarrow.array([2.0] * len(task_id), pyarrow.float64()),
        })
        pyarrow.parquet.write_table(table,
                                    os.path.join(OUT, "predictions.parquet"))
        ok(f"predictions.parquet written with {len(task_id)} rows and the"
           f" frozen columns")
    except Exception as e:                                     # noqa: BLE001
        bad(f"could not write predictions.parquet: {type(e).__name__}: {e}")

    print(f"== EXTRACT-CONTRACT: pass={_pass} fail={_fail} ==", flush=True)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
EOF

echo "wrote research/experiments/extract_contract.py"

cat > "$EXP/baseline_contract.py" <<'EOF'
"""2b-3: the probe's view of the BASELINE mount, asserted from inside the sandbox.

Reports what it SAW rather than what it expected, and prints `present=0|1` in its
summary. The script cannot know whether this run asked for a baseline -- only the
submitter knows that -- so a fixture that assumed either answer would pass for
the wrong reason on half its runs. The NC suite asks for a baseline on one run
and not on the next, and asserts `present=` against what IT requested. That split
is the point: the fixture observes, the suite claims.
"""
import hashlib
import json
import os
import sys

BASELINE = "/baseline"
EXTRACT = "/extract"

_pass = 0
_fail = 0


def ok(msg):
    global _pass
    _pass += 1
    print(f"ok    {msg}", flush=True)


def bad(msg):
    global _fail
    _fail += 1
    print(f"FAIL  {msg}", flush=True)


def canonical(manifest):
    """Byte-for-byte what `baseline.canonical` produces.

    A SECOND implementation, deliberately, and this is the one place in the
    project where duplicating logic is the right call: this runs as
    agent-authored code inside the sandbox, which cannot import the trusted
    module. If the two ever disagree, that disagreement is the finding.
    """
    body = {k: v for k, v in manifest.items() if k != "baseline_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def main():
    present = os.path.isdir(BASELINE)

    if present:
        manifest = None
        try:
            with open(os.path.join(BASELINE, "MANIFEST.json")) as fh:
                manifest = json.load(fh)
            ok("MANIFEST.json is readable and is JSON")
        except Exception as e:                                 # noqa: BLE001
            bad(f"MANIFEST.json is unreadable: {e}")

        # THE ASSERTION THAT MATTERS, and the same one /extract gets: a promoted
        # baseline is immutable, and a run that could write to it would change
        # what a recorded comparison was measured against -- silently, because
        # the hash is computed once at promotion and never recomputed on read.
        try:
            with open(os.path.join(BASELINE, ".probe-write"), "w") as fh:
                fh.write("x")
            bad(f"{BASELINE} is WRITABLE: a promoted baseline can be rewritten")
            os.unlink(os.path.join(BASELINE, ".probe-write"))
        except OSError:
            ok(f"{BASELINE} is not writable")

        if manifest:
            # The identity is a CONTENT key, so it can be verified rather than
            # trusted -- from inside the sandbox, against the bytes actually
            # mounted. Nothing else in this project can check an identity this
            # directly.
            digest = hashlib.sha256(canonical(manifest)).hexdigest()
            declared = manifest.get("baseline_hash")
            if digest == declared:
                ok(f"the baseline hashes to its declared identity {digest[:12]}")
            else:
                bad(f"the mounted baseline hashes to {digest[:12]} but declares"
                    f" {str(declared)[:12]}")

            files = manifest.get("files") or {}
            if not files:
                bad("the manifest lists no files")
            for name, entry in sorted(files.items()):
                target = os.path.join(BASELINE, name)
                try:
                    size = os.path.getsize(target)
                except OSError as e:
                    bad(f"{name}: unreadable ({e})")
                    continue
                # DIGEST, not just size. The manifest carries a sha256 per file
                # and this is the only place it is ever checked against the
                # bytes; a size check would pass on a file of the right length
                # and the wrong content.
                h = hashlib.sha256()
                with open(target, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() == entry.get("sha256") and size > 0:
                    ok(f"{name}: {size} bytes, digest matches the manifest")
                else:
                    bad(f"{name}: digest {h.hexdigest()[:12]} vs manifest"
                        f" {str(entry.get('sha256'))[:12]}, size={size}")

            print(f"    baseline_hash={str(declared)[:12]}"
                  f" days={len(manifest.get('days') or [])}"
                  f" rows={manifest.get('ndjson_rows')}"
                  f" exclude_dates={manifest.get('exclude_dates')}", flush=True)
    else:
        # Absence is a control too: the baseline must not be AMBIENT. A probe
        # that reads no baseline must not find one lying around, or a cohort
        # could compare against data its record does not name.
        ok(f"{BASELINE} is absent, as it must be when none was requested")

    # The extract is there either way -- a probe always has one -- so a missing
    # /extract here means the baseline mount displaced it rather than joining it.
    if os.path.isdir(EXTRACT):
        ok(f"{EXTRACT} is still mounted alongside")
    else:
        bad(f"{EXTRACT} is missing: adding a mount replaced one")

    print(f"== BASELINE-CONTRACT: present={int(present)}"
          f" pass={_pass} fail={_fail} ==", flush=True)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
EOF

echo "wrote research/experiments/baseline_contract.py"

cat <<'NEXT'

Next, in the qf-research checkout, with the AGENT's credential. RE-RUN THIS
GENERATOR AND RE-PUSH if you pushed an earlier revision: the fixture's `DATA`
path was one level short (`/app/trainer/data` rather than
`/app/trainer/trainer/data`), which is the same defect the dispatcher had.

  git checkout -b probe-extract-contract    # or commit to the fixture branch
  git add research/experiments/extract_contract.py \
          research/experiments/baseline_contract.py
  git commit -m '2b-2: probe fixture asserting the extract mount contract'
  git push -u origin HEAD
  git rev-parse HEAD

Then, on the host, with an extract already published:

  sudo -H -u research qf extracts     # copy the `--extract <hash>` line it prints
  sudo -H -u research qf probe --sha <the qf-research sha> \
      --path research/experiments/extract_contract.py \
      --extract <hash, or any unique 8+ hex prefix> --wait

Expect `== EXTRACT-CONTRACT: pass=N fail=0 ==` in `qf logs`, and
predictions.parquet in the run's artifacts/.
NEXT
