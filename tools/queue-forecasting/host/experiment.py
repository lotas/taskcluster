#!/usr/bin/env python3
"""Resolve a config's inputs, then run it. The part an operator used to be.

WHY THIS EXISTS. Running one experiment took an operator holding four facts in
their head: which extract can serve this config's window, which baseline and
contract keep the result comparable to the last one, how much memory the host
allows, and what the resulting hashes were. None of those are judgement -- they
are all derivable -- but until they were derived by something, every run went
through a human pasting hashes, and an agent could not run at all.

WHAT IS STILL JUDGEMENT, and therefore still not here: which experiment to run.
That comes off `experiment-queue.md`, and it is the whole contribution.

THE RESOLUTION RULE, in one sentence: among the inputs that CAN serve this
config, prefer the ones the most already-scored runs used. Comparability beats
freshness -- a run against inputs nobody else used is a number that belongs to
no series, so "the newest extract" is the wrong default even when it works.
Only when nothing published can serve the config does this stop, and then it
prints the exact command that fixes it with every value filled in.

    experiment.py doctor            can this host run an experiment unattended?
    experiment.py plan <config>     what would run, and why those inputs
    experiment.py run <config>      plan, push, probe, evaluate, print the score

`plan` is the honest half of `run`: it resolves and explains without spending
anything, so a disagreement about inputs happens before twenty minutes of
training rather than after.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

WINDOW_KEYS = ("holdout_days", "validation_days", "lookback_days")


class Refused(Exception):
    """A refusal whose text is meant to be read and acted on.

    Distinguished from an unexpected traceback on purpose: everything raised
    this way names a remedy, and `main` prints it without a stack.
    """


# --------------------------------------------------------------------------
# Config reading
# --------------------------------------------------------------------------

def read_config(path):
    """The handful of values this needs from a trainer config.

    Uses PyYAML when importable and a narrow line parser when it is not. The
    fallback exists because this runs as the research user, outside the
    trainer's virtualenv, and refusing to plan an experiment over a missing
    yaml module would put an operator back in the loop for a reason that has
    nothing to do with the experiment.

    The fallback is NOT a yaml parser and does not pretend to be one. It reads
    top-level `key: <scalar>` lines and one nested flag, and anything it cannot
    read with confidence becomes a refusal rather than a default -- a guessed
    window key resolves to an extract the trainer never asks for.
    """
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError as e:
        raise Refused(f"cannot read the config at {path}: {e}")

    raw = None
    try:
        import yaml
        raw = yaml.safe_load(text)
    except ImportError:
        pass
    if isinstance(raw, dict):
        window = {k: raw.get(k) for k in WINDOW_KEYS}
        qctx = bool((raw.get("queue_context_features") or {}).get("enabled"))
        target, model = raw.get("target"), raw.get("model_type")
    else:
        window = {k: _scalar_int(text, k) for k in WINDOW_KEYS}
        qctx = _nested_flag(text, "queue_context_features", "enabled")
        target = _scalar_str(text, "target")
        model = _scalar_str(text, "model_type")

    bad = [k for k, v in window.items()
           if not isinstance(v, int) or isinstance(v, bool) or v < 0]
    if bad:
        raise Refused(
            f"{path} does not state {', '.join(sorted(bad))} as a"
            " non-negative integer, and this refuses to assume a value: the"
            " window keys decide which extracts can serve the config, so a"
            " guessed one resolves to an extract the trainer never asks for.")
    if not target:
        raise Refused(f"{path} names no `target`")
    return {"path": path, "target": target, "model_type": model, "qctx": qctx,
            "cohort_span_days": sum(window.values()), **window}


def _scalar_int(text, key):
    found = re.findall(rf"^{re.escape(key)}:[ \t]*(-?\d+)[ \t]*(?:#.*)?$",
                       text, re.M)
    return int(found[0]) if len(found) == 1 else None


def _scalar_str(text, key):
    found = re.findall(rf"^{re.escape(key)}:[ \t]*([A-Za-z0-9_.\-]+)[ \t]*"
                       rf"(?:#.*)?$", text, re.M)
    return found[0] if len(found) == 1 else None


def _nested_flag(text, parent, child):
    """`parent:` followed by an indented `child: true`, nothing cleverer.

    Scans only the indented block directly following the parent key, so a
    same-named child under a different parent cannot satisfy it.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.rstrip() != f"{parent}:":
            continue
        for follow in lines[i + 1:]:
            if follow.strip() and not follow[:1].isspace():
                break                           # left the block
            match = re.match(rf"\s+{re.escape(child)}:\s*(\S+)", follow)
            if match:
                return match.group(1).lower() in ("true", "yes", "on", "1")
    return False


# --------------------------------------------------------------------------
# Resolution -- pure functions over what `qf` reports
# --------------------------------------------------------------------------

def parse_day(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z",
                                                                   "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(
        tzinfo=datetime.timezone.utc)


def cohort_train_start(config, as_of):
    """Exactly `run_cohort.check_window`'s arithmetic, deliberately.

    If these two disagree, this resolver picks an extract that the check inside
    the container then refuses -- the current failure mode with an extra step.
    """
    parsed = parse_day(as_of)
    return None if parsed is None else parsed - datetime.timedelta(
        days=config["cohort_span_days"])


def extract_can_serve(config, extract):
    """Why this extract can or cannot run this config; empty means it can.

    Returns REASONS rather than a boolean because the explanation is the
    interesting output: whoever asks "why not the newest one" deserves the
    answer printed next to the candidate it lost to.
    """
    reasons = []
    if extract.get("target") != config["target"]:
        reasons.append(f"target is {extract.get('target')!r},"
                       f" config wants {config['target']!r}")
    as_of = parse_day(extract.get("as_of_date"))
    train_start = parse_day(extract.get("train_start"))
    if as_of is None or train_start is None:
        reasons.append("window is unreadable")
        return reasons
    need = cohort_train_start(config, extract.get("as_of_date"))
    if need is None or need < train_start:
        reasons.append(
            f"window starts {train_start.date()} but this cohort needs"
            f" {need.date() if need else '?'} (as_of {as_of.date()} minus"
            f" {config['cohort_span_days']}d)")
    if config["qctx"]:
        columns = (extract.get("columns") or {}).get("qctx_runs") or []
        if "task_created" not in columns:
            reasons.append("qctx_runs has no `task_created`, and this config"
                           " enables queue_context_features")
    return reasons


def usage_counts(history):
    """How many scored runs used each input hash.

    What makes comparability computable instead of remembered. Counted only
    from evaluations that SUCCEEDED: a pinned input on a failed run is not
    evidence that anything was measured against it.
    """
    counts = {"extract": {}, "baseline": {}, "contract": {}}
    for row in history or []:
        pins = row.get("pins") or {}
        for key, pin in (("extract", "request_hash"),
                         ("baseline", "baseline_hash"),
                         ("contract", "contract_hash")):
            value = pins.get(pin)
            if value:
                counts[key][value] = counts[key].get(value, 0) + 1
    return counts


def choose_extract(config, extracts, counts):
    """The most-used, then narrowest, extract that can serve this config.

    Ordering, most significant first:

      1. how many scored runs already used it   -- comparability
      2. narrower window                        -- less memory, and closer to
                                                   whatever the series used
      3. newer snapshot                         -- a tiebreak, not a goal

    Freshness is LAST on purpose. `qf extracts` is not a menu and "newer
    exists" is not a reason: two results are comparable only if they share
    inputs, and comparing results is the entire job.
    """
    ranked, rejected = [], []
    for extract in extracts or []:
        reasons = extract_can_serve(config, extract)
        if reasons:
            rejected.append((extract, reasons))
            continue
        as_of = parse_day(extract.get("as_of_date"))
        train_start = parse_day(extract.get("train_start"))
        ranked.append((-counts.get(extract.get("request_hash"), 0),
                       (as_of - train_start).days,
                       _newest_first(extract),
                       extract.get("request_hash") or "",
                       extract))
    if not ranked:
        raise Refused(no_extract_message(config, rejected))
    ranked.sort(key=lambda row: row[:4])
    return ranked[0][4], rejected, [row[4] for row in ranked[1:]]


def _newest_first(extract):
    """Newest-first sort key, with unknown snapshots sorting last."""
    parsed = parse_day(extract.get("snapshot_start_ts"))
    return -parsed.timestamp() if parsed else float("inf")


def no_extract_message(config, rejected):
    """The refusal that used to be a placeholder in a chat message.

    Every value in the printed command is computed, because the failure this
    replaces was an operator being told to substitute one.
    """
    lines = [f"no published extract can serve {config['path']}.",
             "  candidates, and why each was rejected:"]
    for extract, reasons in sorted(rejected,
                                   key=lambda r: r[0].get("request_hash") or ""):
        lines.append(f"    {(extract.get('request_hash') or '?')[:12]}"
                     f"  {extract.get('train_start')}"
                     f"..{extract.get('as_of_date')}")
        lines += [f"        - {reason}" for reason in reasons]

    # The window this config needs, anchored on the as_of of the newest extract
    # with the right target, so a new extract stays as close to the existing
    # series as its window allows instead of starting an unrelated one.
    same_target = [e for e, _ in rejected
                   if e.get("target") == config["target"]
                   and parse_day(e.get("as_of_date"))]
    if not same_target:
        return "\n".join(lines)
    anchor = max(same_target, key=lambda e: parse_day(e["as_of_date"]))
    as_of = parse_day(anchor["as_of_date"])
    need = as_of - datetime.timedelta(days=config["cohort_span_days"])
    lookback = anchor.get("lookback_days")
    lines += [
        "",
        "  the extract this config needs, anchored on the same as_of as"
        f" {(anchor.get('request_hash') or '?')[:12]} so the result stays"
        " comparable to that series:",
        "",
        f"    qf extract --target {config['target']} \\",
        f"        --train-start {need.date()} \\",
        f"        --as-of {as_of.date()} \\",
        f"        --lookback-days {lookback if lookback is not None else 30} \\",
        f"        --note '{os.path.basename(config['path'])}:"
        f" {config['cohort_span_days']}d cohort' --wait",
    ]
    if lookback is None:
        lines.append("    # lookback_days is not published by this extract"
                     " (pre-2026-08-31 manifest); 30 is the qf default, and a"
                     " wrong value produces a different request_hash rather"
                     " than an error.")
    lines += ["",
              "  That is an OPERATOR action: a new extract is a new"
              " request_hash, and a new hash starts a new series."]
    return "\n".join(lines)


def choose_baseline(baselines, counts):
    """The most-used baseline that is not broken; newest as a tiebreak."""
    usable = [b for b in (baselines or []) if not b.get("broken")]
    if not usable:
        raise Refused(
            "no usable promoted baseline: every published one is flagged"
            " broken, or none is published. `promote-baseline.sh` publishes"
            " one, and that is an operator action.")
    usable.sort(key=lambda b: (-counts.get(b.get("baseline_hash"), 0),
                               _reverse(str(b.get("promoted_at") or "")),
                               b.get("baseline_hash") or ""))
    return usable[0]


def choose_contract(target, contracts, counts):
    """The most-used contract for this target."""
    usable = [c for c in (contracts or []) if c.get("target") in (None, target)]
    if not usable:
        raise Refused(
            f"no published contract for target {target!r}."
            " `instantiate-contract.sh` pins a template to a promoted"
            " baseline_hash, and that is an operator action.")
    usable.sort(key=lambda c: (-counts.get(c.get("contract_hash"), 0),
                               _reverse(str(c.get("created_at") or "")),
                               c.get("contract_hash") or ""))
    return usable[0]


class _reverse:
    """Descending order for a string inside an ascending tuple sort key.

    Written out rather than achieved with `reverse=True`, which would invert
    every field including the usage count that has to stay descending.
    """

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __eq__(self, other):
        return self.value == other.value


def plan(config, extracts, baselines, contracts, history):
    """Everything a run needs, plus why each input was chosen."""
    counts = usage_counts(history)
    extract, rejected, runners_up = choose_extract(config, extracts,
                                                   counts["extract"])
    train_start = cohort_train_start(config, extract.get("as_of_date"))
    return {
        "config": config,
        "extract": extract,
        "baseline": choose_baseline(baselines, counts["baseline"]),
        "contract": choose_contract(config["target"], contracts,
                                    counts["contract"]),
        "as_of": extract.get("as_of_date"),
        "cohort_train_start": train_start.date().isoformat() if train_start
                              else None,
        "scored_runs_here": counts["extract"].get(extract.get("request_hash"),
                                                  0),
        "rejected": rejected,
        "runners_up": runners_up,
    }


# --------------------------------------------------------------------------
# Talking to `qf`, and to git
# --------------------------------------------------------------------------

def qf(*args, timeout=1800):
    """One `qf` call. Returns (ok, parsed-or-text).

    `qf` is on PATH for the research user. Nothing here adds `sudo`: this
    program is meant to run AS the identity that owns the experiment, and a
    tool that elevates is a tool an agent must not be given.
    """
    try:
        done = subprocess.run(["qf", *args], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise Refused("`qf` is not on PATH. This must run as an identity that"
                      " has it -- normally the research user.")
    except subprocess.TimeoutExpired:
        return False, f"`qf {' '.join(args)}` timed out after {timeout}s"
    text = (done.stdout or "") + (done.stderr or "")
    if "--json" in args and done.returncode == 0:
        try:
            return True, json.loads(done.stdout)
        except ValueError:
            return False, f"`qf {' '.join(args)}` returned unparseable JSON"
    return done.returncode == 0, text


def inventory(limit=200):
    """Everything the resolver needs, in one place.

    `history` costs one `qf status` per scored evaluation, which is the same
    shape `results.sh` pays: `list` carries no pins, and the pins are what say
    which inputs a result belongs to.
    """
    out = {}
    for name, op in (("extracts", "extracts"), ("baselines", "baselines"),
                     ("contracts", "contracts")):
        ok, body = qf(op, "--json", timeout=60)
        if not ok:
            raise Refused(f"cannot read `qf {op}`: {body}")
        out[name] = body.get(name) or []

    ok, body = qf("list", "--limit", str(limit), "--json", timeout=120)
    if not ok:
        raise Refused(f"cannot read `qf list`: {body}")
    history = []
    for job in body.get("jobs") or []:
        if job.get("kind") != "evaluate" or job.get("state") != "SUCCEEDED":
            continue
        ok, status = qf("status", job["run_id"], "--json", timeout=60)
        if ok:
            history.append(status.get("job") or status)
    out["history"] = history
    return out


def git(workspace, *args, timeout=300):
    done = subprocess.run(["git", "-C", workspace, *args],
                          capture_output=True, text=True, timeout=timeout,
                          # A push that stops for a passphrase or a username
                          # would hang forever inside an agent with no
                          # terminal. Made to FAIL instead, so `doctor` can
                          # report it as the blocker it is.
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                               "GIT_ASKPASS": "/bin/false",
                               "SSH_ASKPASS": "/bin/false"})
    return done.returncode == 0, ((done.stdout or "") + (done.stderr or "")).strip()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def workspace_path(given):
    return os.path.abspath(
        given or os.environ.get("QF_RESEARCH")
        or os.path.expanduser("~/qf-research"))


TRUSTED_TRAINER = "/srv/queue-forecasting/tools/queue-forecasting/trainer"


def trainer_drift(workspace, trusted=TRUSTED_TRAINER):
    """Which trainer files in the workspace differ from the deployed ones.

    REPORTED, NOT REFUSED, and the distinction is the whole point. In this loop
    an edit under `trainer/` IS the experiment -- refusing on difference would
    refuse every real run. What is dangerous is difference the agent did not
    make: nothing syncs the trusted trainer into the research user's checkout
    (`first-probe.sh` syncs the OPERATOR's), so a workspace can sit behind
    deployed code indefinitely and every result it produces is attributed to
    the wrong thing.

    So this names the files and lets a reader decide, which is the only
    honest option without a way to tell an intended edit from a stale one.
    """
    import hashlib
    interesting = []
    for root, subdirs, names in os.walk(os.path.join(trusted)):
        subdirs[:] = [d for d in subdirs
                      if d not in ("__pycache__", ".venv", "data", "env")]
        for name in names:
            if name.endswith((".py", ".yaml", ".yml")):
                full = os.path.join(root, name)
                interesting.append(os.path.relpath(full, trusted))
    if not interesting:
        return None                 # nothing to compare against; say so upstream

    def digest(path):
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return None

    differing = []
    for relative in sorted(interesting):
        here = digest(os.path.join(workspace, "trainer", relative))
        there = digest(os.path.join(trusted, relative))
        if here != there:
            differing.append(relative if here else f"{relative} (MISSING)")
    return differing


def push_fix(output):
    """The remedy for a failed push, chosen by WHICH failure it was.

    These have opposite fixes and similar-looking output, and conflating them
    sends an operator to rotate a credential that was fine. Observed on
    2026-08-31: a push failed with "Failed to connect to github.com port 443
    after 5 ms" and was reported as a credential problem. It was egress -- the
    research user reaches the network only through tinyproxy, the proxy
    variables live in `~/.profile`, and a non-login shell reads neither that nor
    `.bashrc`, so git bypassed the proxy and nftables refused it. Five
    milliseconds to "fail to connect" is a local refusal, not a network.
    """
    text = (output or "").lower()
    if ("could not connect" in text or "failed to connect" in text
            or "could not resolve host" in text or "connection refused" in text):
        return ("EGRESS, not a credential. This user reaches the network only"
                " through tinyproxy, and the proxy variables live in"
                " ~/.profile -- which a non-login shell does not read. Run"
                " through `bash -lc` (experiment.sh does), or source"
                " ~/.profile.d-proxy. If the proxy IS set, the host may not be"
                " on the allowlist: /etc/tinyproxy/allowlist.txt.")
    # `denied` on its own, and checked AFTER connectivity so a refused
    # connection never lands here. Git says "Permission to <repo> denied" and
    # "Permission denied (publickey)" -- matching the joined phrase misses the
    # first, which is the one a token with the wrong scope produces.
    if ("authentication failed" in text or "terminal prompts disabled" in text
            or "could not read username" in text or "denied" in text
            or "403" in text or "401" in text):
        return ("A CREDENTIAL this user does not have. With"
                " GIT_TERMINAL_PROMPT=0 a credential that needs typing fails"
                " instead of hanging. Give this user one it owns -- a token in"
                " its own git credential store, or a passphrase-less key"
                " scoped to qf-research.")
    return ("Unattended runs need a push that completes with no input. Read"
            " the git output above: connectivity and credentials fail"
            " differently and have opposite fixes.")


def cmd_doctor(args):
    """Every precondition for an unattended run, and the fix for each.

    WRITTEN BECAUSE THE ANSWER WAS UNKNOWABLE FROM OUTSIDE. Whether this host
    can run an experiment without an operator depends on facts only the host
    has -- whether the agent's checkout exists, whether its push is
    non-interactive, whether anything is published to run against. Guessing
    them produced commands with placeholders in them.
    """
    failures = []

    def check(label, ok, detail="", fix=""):
        """A BLOCKER when false: unattended runs are impossible until it holds."""
        print(f"  {'ok  ' if ok else 'FAIL'} {label}"
              + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append((label, fix))
            if fix:
                print(f"       -> {fix}")

    def note(label, ok, detail="", guidance=""):
        """Worth knowing, never a blocker.

        Kept separate from `check` because conflating them makes the summary
        lie in the direction that matters: a dirty workspace and an edited
        trainer are what an experiment LOOKS like, and reporting either as
        "unattended runs are not possible" would train a reader to ignore the
        line that actually stops them.
        """
        print(f"  {'ok  ' if ok else 'note'} {label}"
              + (f"  {detail}" if detail else ""))
        if not ok and guidance:
            print(f"       -- {guidance}")

    print("== qf")
    ok, body = qf("ping", timeout=30)
    check("the dispatcher answers", ok, "" if ok else body.strip()[:200],
          "systemctl status qf-dispatch")

    print("== published inputs")
    try:
        inv = inventory(limit=args.limit)
    except Refused as e:
        check("inventory readable", False, str(e)[:200])
        inv = {"extracts": [], "baselines": [], "contracts": [], "history": []}
    check("extracts", bool(inv["extracts"]), f"{len(inv['extracts'])} published",
          "qf extract --target wait_time ... (operator)")
    usable_baselines = [b for b in inv["baselines"] if not b.get("broken")]
    check("promoted baselines", bool(usable_baselines),
          f"{len(usable_baselines)} usable of {len(inv['baselines'])}",
          "promote-baseline.sh (operator)")
    check("contracts", bool(inv["contracts"]),
          f"{len(inv['contracts'])} published",
          "instantiate-contract.sh (operator)")
    note("scored history", bool(inv["history"]),
         f"{len(inv['history'])} scored evaluations",
         "with no history every input ranks by window, not by use")

    print("== the agent's workspace")
    workspace = workspace_path(args.workspace)
    print(f"  at {workspace}")
    is_repo = os.path.isdir(os.path.join(workspace, ".git")) or os.path.isfile(
        os.path.join(workspace, ".git"))
    check("is a git checkout", is_repo, "",
          f"clone qf-research to {workspace} as this user, with a credential"
          " this user owns")
    if is_repo:
        ok, who = git(workspace, "config", "--get", "remote.origin.url")
        check("has an origin", ok, who if ok else "",
              "git remote add origin <qf-research>")
        writable = os.access(workspace, os.W_OK)
        check("is writable by this user", writable,
              f"uid={os.getuid()}",
              "an agent cannot commit into a checkout it does not own")
        ok, out = git(workspace, "push", "--dry-run", "--porcelain", timeout=120)
        check("push works without a prompt", ok,
              out.splitlines()[-1][:160] if out else "", push_fix(out))
        drift = trainer_drift(workspace, args.trusted_trainer)
        if drift is None:
            note("trainer comparable to the deployed one", False,
                 f"nothing readable at {args.trusted_trainer}",
                 "nothing can tell whether this workspace trains current"
                 " code; point --trusted-trainer at a readable copy")
        else:
            note("trainer matches the deployed one", not drift,
                  f"{len(drift)} file(s) differ" if drift else "",
                  ("expected when the agent's own edit IS the experiment;"
                   " stale otherwise. differing: "
                   + ", ".join(drift[:6])
                   + (" ..." if len(drift) > 6 else "")) if drift else "")

        ok, out = git(workspace, "status", "--porcelain")
        note("clean tree", ok and not out.strip(),
             f"{len(out.splitlines())} changed" if out.strip() else "",
             "`run` commits what it finds, which is the point")

    print("== memory")
    print(f"  probe default {args.mem}; a refusal names the host ceiling and"
          " `run` retries once at it")

    print()
    if failures:
        print(f"{len(failures)} blocker(s). Unattended runs are not possible"
              " until the ones marked FAIL above are cleared.")
        return 1
    print("this host can run an experiment unattended.")
    return 0


def render_plan(resolved):
    config, extract = resolved["config"], resolved["extract"]
    out = [f"config    {config['path']}",
           f"          {config['cohort_span_days']}d cohort"
           f" (holdout {config['holdout_days']} + validation"
           f" {config['validation_days']} + lookback"
           f" {config['lookback_days']}), qctx={'yes' if config['qctx'] else 'no'},"
           f" model={config['model_type']}",
           f"extract   {extract.get('request_hash')}",
           f"          {extract.get('train_start')}"
           f"..{extract.get('as_of_date')}"
           f"  gen={extract.get('generation')}"
           f"  lookback={extract.get('lookback_days')}",
           f"          cohort needs train_start"
           f" <= {resolved['cohort_train_start']}",
           f"baseline  {resolved['baseline'].get('baseline_hash')}",
           f"contract  {resolved['contract'].get('contract_hash')}"]

    scored = resolved["scored_runs_here"]
    if scored:
        out.append(f"\n{scored} scored run(s) already used this extract, so a"
                   " result here is comparable to them.")
    else:
        out.append("\nNO scored run has used this extract yet, so a result"
                   " here is comparable to NOTHING. Run the config you intend"
                   " to compare against on it too, or the number belongs to no"
                   " series.")
    if resolved["runners_up"]:
        out.append("\nalso able to serve this config, and not chosen:")
        for other in resolved["runners_up"]:
            out.append(f"  {(other.get('request_hash') or '?')[:12]}"
                       f"  {other.get('train_start')}"
                       f"..{other.get('as_of_date')}")
    if resolved["rejected"]:
        out.append("\ncannot serve this config:")
        for other, reasons in resolved["rejected"]:
            out.append(f"  {(other.get('request_hash') or '?')[:12]}"
                       f"  {'; '.join(reasons)}")
    return "\n".join(out)


def cmd_plan(args):
    config = read_config(config_path(workspace_path(args.workspace),
                                     args.config))
    resolved = plan(config, **inventory(limit=args.limit))
    print(render_plan(resolved))
    return 0


def point_run_cohort_at(workspace, config):
    """Rewrite the `CONFIG = "..."` constant the probe will train.

    The probe trains a COMMIT, and the config it trains is a constant inside
    that commit -- so selecting a config is an edit, not an argument.
    """
    path = os.path.join(workspace, "research", "experiments", "run_cohort.py")
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError as e:
        raise Refused(f"cannot read {path}: {e}")
    patched, count = re.subn(r'^CONFIG = ".*"$', f'CONFIG = "{config}"', text,
                             count=1, flags=re.M)
    if count != 1:
        raise Refused(f"{path} has no single `CONFIG = \"...\"` line to point"
                      " at a config; refusing to guess where it went")
    if patched != text:
        with open(path, "w") as fh:
            fh.write(patched)
    return patched != text


def config_path(workspace, given):
    """A config named the way `run_cohort.py` names it, found on disk.

    `CONFIG` inside the probe is relative to `trainer/`, so that is what the
    caller passes and what gets committed -- but this process has to READ the
    file to compute the window, and it is not running in the container where
    `trainer/` is the working directory.
    """
    if os.path.isabs(given) or os.path.exists(given):
        return given
    return os.path.join(workspace, "trainer", given)


def cmd_run(args):
    workspace = workspace_path(args.workspace)
    config = read_config(config_path(workspace, args.config))
    resolved = plan(config, **inventory(limit=args.limit))
    print(render_plan(resolved))
    print()
    if args.dry_run:
        print("--dry-run: stopping before the push")
        return 0

    point_run_cohort_at(workspace, args.config)
    ok, dirty = git(workspace, "status", "--porcelain")
    if not ok:
        raise Refused(f"cannot read the workspace state: {dirty}")
    if dirty.strip():
        note = args.note or f"experiment: {args.config}"
        for command in (("add", "-A"),
                        ("-c", "core.hooksPath=/dev/null", "commit", "-q",
                         "-m", note)):
            ok, out = git(workspace, *command)
            if not ok:
                raise Refused(f"git {command[0]} failed: {out}")
        print(f"committed: {note}")
    ok, out = git(workspace, "push", "-q", timeout=300)
    if not ok:
        raise Refused("push failed, and an agent cannot answer a prompt:\n"
                      f"  {out}\nRun `experiment.py doctor` -- this is the"
                      " autonomy blocker it checks for.")
    ok, sha = git(workspace, "rev-parse", "HEAD")
    if not ok:
        raise Refused(f"cannot read HEAD: {sha}")
    print(f"sha       {sha}")

    note = f"cfg={args.config}" + (f" | {args.note}" if args.note else "")
    probe = ["probe", "--sha", sha,
             "--path", "research/experiments/run_cohort.py",
             "--extract", resolved["extract"]["request_hash"],
             "--baseline", resolved["baseline"]["baseline_hash"],
             "--note", note, "--wait"]
    mem = args.mem
    ok, out = qf(*probe, "--mem", mem, timeout=args.timeout)
    ceiling = re.search(r"exceeds the host ceiling of (\d+m)", out or "")
    if not ok and ceiling:
        # The ceiling is only knowable by being refused by it, so being
        # refused once is not a reason to end the run. Retried exactly once,
        # at the value the refusal named, and said out loud.
        mem = ceiling.group(1)
        print(f"probe refused {args.mem}; retrying at the host ceiling {mem}")
        ok, out = qf(*probe, "--mem", mem, timeout=args.timeout)
    print(out.rstrip())
    if not ok:
        return 1

    run_id = _last_run_id(out, "probe-")
    if not run_id:
        raise Refused("the probe succeeded but printed no run id to evaluate")
    ok, out = qf("evaluate", "--run", run_id,
                 "--contract", resolved["contract"]["contract_hash"],
                 "--note", note, "--wait", timeout=args.timeout)
    print(out.rstrip())
    return 0 if ok else 1


def _last_run_id(text, prefix):
    found = re.findall(rf"\b{prefix}[0-9A-Za-z-]+\b", text or "")
    return found[-1] if found else None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="resolve a config's inputs and run it")
    parser.add_argument("--workspace", help="the agent's qf-research checkout"
                        " (default $QF_RESEARCH, else ~/qf-research)")
    parser.add_argument("--limit", type=int, default=200,
                        help="how much history to read for usage counts")
    parser.add_argument("--mem", default="20g")
    parser.add_argument("--trusted-trainer", default=TRUSTED_TRAINER,
                        help="the deployed trainer to compare the workspace"
                             " against")
    parser.add_argument("--timeout", type=int, default=5400)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="can this host run an experiment unattended?")
    for name, help_text in (("plan", "resolve and explain, spending nothing"),
                            ("run", "plan, push, probe, evaluate, score")):
        one = sub.add_parser(name, help=help_text)
        one.add_argument("config", help="path under trainer/, e.g."
                         " configs/wait_qctx_d_priority_flow.yaml")
        one.add_argument("--note", help="what this experiment tests")
        if name == "run":
            one.add_argument("--dry-run", action="store_true",
                             help="stop after planning")
    args = parser.parse_args(argv)
    try:
        return {"doctor": cmd_doctor, "plan": cmd_plan,
                "run": cmd_run}[args.command](args)
    except Refused as e:
        print(f"\nrefused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
