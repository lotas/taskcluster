# Research-loop deadlock escape — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a repeatedly-rejected write-up from livelocking the research loop, and make an auto-PAUSE reach a human and be releasable by closing a GitHub issue.

**Architecture:** Four independent layers, in dependency order. `frontier.py` gains a committed-content reader and a `recorded | unrecorded | retired` handling state, which is what every other layer steers on. `tick.sh` splits its one disagreement counter into a research-drift counter (suppressed for a repeat of the same *resolved, unrecorded* target) and an infrastructure counter (advanced by anything that is not a usable verdict). `tick-prompt.md` gains a required `**Target run:**` field and a reduced retry template. `pause-issue.sh` files a GitHub issue when PAUSE is written and resumes when an allowlisted human closes it.

**Tech Stack:** Python 3 (stdlib only, `unittest`), bash, `git` plumbing, `gh` CLI, systemd.

**Spec:** `tools/queue-forecasting/research-loop-deadlock-design.md`. Read §1–§5 before Task 1; every task below cites the section it implements.

---

## Environment notes — read before starting

**`git commit` is refused in the dev container** (`ERROR: Commits are disabled in devtainer (read-only mode)`). This is why the existing `tests/test_frontier.py:_journal` helper uses `git add` with no commit. Task 1 moves the code to HEAD reads, so its tests must create commits with plumbing, which does work:

```bash
git -C "$root" add -A
tree=$(git -C "$root" write-tree)
commit=$(git -C "$root" commit-tree "$tree" -m fixture)
git -C "$root" update-ref refs/heads/main "$commit"
```

**The `cat-file` path trap this plan exists to avoid**, reproduced:

```console
$ git -C journal ls-tree -r HEAD
100644 blob 6178079…	escalations/y.md
100644 blob 7898192…	x.md
$ git -C journal cat-file -p HEAD:x.md
fatal: path 'x.md' does not exist in 'HEAD'
```

`ls-tree` run with `-C <journal-dir>` prints paths relative to that directory; `cat-file`'s path syntax resolves from the repository root, where the file is `journal/x.md`. **Always feed blob object ids to `cat-file --batch`, never paths.**

**Test commands** (from `tools/queue-forecasting/host`):

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v     # all python tests
python3 tests/test_frontier.py -v                            # one file
./tests/test_tick.sh                                         # shell
./tests/test_unit_drift.sh
```

**Do not commit unless the user asks.** This repository's owner handles commits; the "Commit" steps below say what to stage and what message to use, and you run them only on request.

---

## File structure

| Path | Change | Responsibility |
|---|---|---|
| `host/research-loop/frontier.py` | modify | committed-content reads, target/kind/waiver parsing, `handling` tri-state, new row fields, report surfaces |
| `host/research-loop/tick.sh` | modify | two counters, suppressible predicate, retry mode, usable verdict, escalation kind line, pause-issue wiring |
| `host/research-loop/pause-issue.sh` | **create** | `open` / `check` against the GitHub issue; the only file that talks to `gh` |
| `host/research-loop/pause-resume-allowlist.txt` | **create** | GitHub logins permitted to release a pause |
| `host/research-loop/tick-prompt.md` | modify | `**Target run:**` field, retired rows excluded from actions 1–2, reduced retry template |
| `host/research-loop/qf-tick.service` | modify | `QF_TICK_MAX_VERIFIER_FAILS`, `QF_PAUSE_ISSUE_REPO`, `QF_FRONTIER_RETIRE_AFTER` |
| `host/phase2-setup.sh` | modify | effective-`QF_*`-environment drift check |
| `host/tests/test_frontier.py` | modify | committed reads, targets, kinds, waivers, retirement, new fields |
| `host/tests/test_tick.sh` | modify | counters, suppressible predicate, retry mode, resume ordering |
| `host/tests/test_pause_issue.sh` | **create** | `pause-issue.sh` against a stubbed `gh` |
| `host/tests/test_unit_drift.sh` | modify | effective-environment comparison |
| `host/research-loop/README.md` | modify | operator docs: token, allowlist, how to release a pause, how to waive a retirement |

---

## Task 1: Committed-content reads (§1.1)

**Files:**
- Modify: `host/research-loop/frontier.py:167-234` (replace `_tracked_journal_files`, rewrite the body of `journaled_run_ids`)
- Test: `host/tests/test_frontier.py:455-522`

- [ ] **Step 1: Give the test helper real commits**

Replace `_journal` (`tests/test_frontier.py:455-477`) with a version that commits by plumbing, and can be told not to:

```python
    def _journal(self, files, commit=True, then_edit=None):
        """A journal directory inside a real git repo, committed by plumbing.

        `git commit` IS REFUSED IN THE DEV CONTAINER, so the fixture uses
        write-tree/commit-tree/update-ref. `commit=False` leaves the files
        staged but unreachable from HEAD; `then_edit` rewrites a file in the
        WORKING TREE after the commit, which is the tamper the reader must
        ignore.
        """
        import subprocess
        import tempfile
        root = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", root],
                       capture_output=True)
        d = os.path.join(root, "journal")
        os.makedirs(os.path.join(d, "escalations"), exist_ok=True)
        os.makedirs(os.path.join(d, "waivers"), exist_ok=True)
        for name, body in files.items():
            path = os.path.join(d, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True)
        if commit:
            def git(*args):
                done = subprocess.run(["git", "-C", root, *args],
                                      capture_output=True)
                assert done.returncode == 0, done.stderr
                return done.stdout.decode().strip()
            tree = git("write-tree")
            head = git("commit-tree", tree, "-m", "fixture")
            git("update-ref", "refs/heads/main", head)
        for name, body in (then_edit or {}).items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        return d
```

- [ ] **Step 2: Write the failing tests**

Add to the same class:

```python
    def test_a_working_tree_edit_to_a_committed_entry_is_ignored(self):
        # THE TAMPER THIS CHANGE EXISTS TO STOP. The leader shares the uid, so
        # it can rewrite a committed entry; only HEAD counts.
        d = self._journal(
            {"20260831T140000Z.md": "wrote up eA-1\n"},
            then_edit={"20260831T140000Z.md":
                       "cites evaluate-20260904T090941Z-0a217f40da8f-6446\n"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_a_staged_but_uncommitted_entry_does_not_count(self):
        d = self._journal({"20260831T140000Z.md":
                           "evaluate-20260904T090941Z-0a217f40da8f-6446\n"},
                          commit=False)
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_a_committed_entry_in_the_journal_root_counts(self):
        rid = "evaluate-20260904T090941Z-0a217f40da8f-6446"
        d = self._journal({"20260831T140000Z.md": f"wrote up `{rid}` today\n"})
        self.assertEqual(F.journaled_run_ids(d), {rid})

    def test_blobs_resolve_from_a_journal_subdirectory(self):
        # REGRESSION FOR THE cat-file PATH TRAP: `ls-tree -C journal` prints
        # `escalations/x.md`, but `cat-file HEAD:escalations/x.md` resolves from
        # the REPO ROOT and fails. Blob oids have no prefix to get wrong.
        d = self._journal({"escalations/20260904T101126Z.md": "body\n"})
        blobs = F._committed_blobs(d, "escalations")
        self.assertEqual(blobs, {"20260904T101126Z.md": "body\n"})

    def test_no_repository_counts_nothing(self):
        import tempfile
        d = os.path.join(tempfile.mkdtemp(), "journal")
        os.makedirs(d)
        self.assertIsNone(F._committed_blobs(d))
        self.assertEqual(F.journaled_run_ids(d), set())
```

- [ ] **Step 3: Run them and watch them fail**

Run: `python3 tests/test_frontier.py -v -k committed`
Expected: FAIL — `AttributeError: module 'frontier' has no attribute '_committed_blobs'`, and the working-tree-edit test fails because the current reader returns the tampered id.

- [ ] **Step 4: Replace `_tracked_journal_files` with `_committed_blobs`**

Delete `_tracked_journal_files` (`frontier.py:167-190`) and add:

```python
def _committed_blobs(journal_dir, subdir=None):
    """`{basename: text}` for every `.md` blob committed at HEAD, one namespace.

    `subdir=None` means the flat journal root; `subdir="escalations"` means that
    directory and no deeper. `None` is returned when the question cannot be
    answered -- no repository, no git, no HEAD, a malformed listing or a failed
    call -- and EVERY caller must read that as "nothing counts" rather than
    "everything counts": the alternative silently retires results.

    BLOB OIDS, NEVER PATHS. `ls-tree` run with `-C <journal_dir>` prints paths
    relative to that directory, while `cat-file`'s path syntax resolves from the
    repository root, so `HEAD:<path>` from here is `fatal: path ... does not
    exist in 'HEAD'`. An oid needs no prefix and cannot be misresolved.
    """
    import subprocess
    try:
        listed = subprocess.run(
            ["git", "-C", journal_dir, "ls-tree", "-z", "-r", "HEAD"],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None

    oids, names = [], []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        meta, tab, path = raw.partition(b"\t")
        if not tab:
            return None                     # not the documented format: refuse
        fields = meta.split(b" ")
        if len(fields) != 3:
            return None
        if fields[1] != b"blob":
            continue
        name = path.decode("utf-8", "replace")
        if not name.endswith(".md"):
            continue
        # DEPTH IS ENFORCED ON THE RETURNED PATHS, not by a pathspec: a pathspec
        # of `.` still returns nested entries (verified), so the filter has to
        # be here or `escalations/` would leak into the journal root's reader.
        parts = name.split("/")
        if subdir is None:
            if len(parts) != 1:
                continue
        elif len(parts) != 2 or parts[0] != subdir:
            continue
        oids.append(fields[2].decode("ascii", "replace"))
        names.append(parts[-1])

    if not oids:
        return {}
    try:
        done = subprocess.run(
            ["git", "-C", journal_dir, "cat-file", "--batch"],
            input=("\n".join(oids) + "\n").encode(),
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None

    return _parse_batch(done.stdout, names)
```

and the framing in its own testable unit — **not inline**, because a parser only
reachable by monkeypatching is a parser nothing tests:

```python
def _parse_batch(buf, names):
    """Pair `cat-file --batch` output with the names it was asked about.

    `--batch` answers IN INPUT ORDER, so pairing by index is sound. Each answer
    is `<oid> SP <type> SP <size> LF <bytes> LF`.

    `None` ON ANY FRAMING SURPRISE, and that is the whole point: slicing past
    the end of `bytes` does NOT raise in Python, so an unchecked parser answers
    a truncated response with content spliced out of two records -- a silently
    wrong answer about whether a result was already written up. Found by review
    of this plan's own first draft, which had exactly that bug: a declared
    size of 4 with 3 bytes present returned `'AB\nd'`.
    """
```

It returns `None` when: no newline remains where a header is expected; the
header does not split into exactly three space-separated fields (which is also
what `--batch` prints for a missing object, `<oid> missing`); the size is not an
integer; the buffer does not hold `size` content bytes **plus** the trailing
newline; or the byte where that newline must be is not `\n` — the last is the
assertion that catches a wrong size even when enough bytes happen to follow.

Test it directly with bytes, no git: a well-formed two-record buffer; a declared
size longer than the remaining bytes; a size one byte short so the newline lands
on content; a two-field header; a non-integer size; an empty buffer with names
expected.

- [ ] **Step 5: Rewrite `journaled_run_ids` to use it**

Keep the whole docstring, appending one paragraph, and replace the body:

```python
def journaled_run_ids(journal_dir):
    """Every run id cited by a RECORDED journal entry.

    ... (keep the existing docstring verbatim) ...

    RETIREMENT IS NOT A RELAXATION OF THIS. `escalation_targets` (§1.3 of
    research-loop-deadlock-design.md) counts escalations separately and marks a
    run `retired` in its own state; an escalated entry still never makes a run
    look recorded here.
    """
    blobs = _committed_blobs(journal_dir)
    if blobs is None:
        return set()
    seen = set()
    for name, text in blobs.items():
        if name == "PENDING.md":            # cannot be committed; cheap guard
            continue
        seen.update(_RUN_ID.findall(text))
    return seen
```

- [ ] **Step 6: Run the whole file**

Run: `python3 tests/test_frontier.py -v`
Expected: PASS, all of it. The pre-existing journal tests still pass because `_journal` now commits by default.

- [ ] **Step 7: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/frontier.py \
        tools/queue-forecasting/host/tests/test_frontier.py
git commit -m "frontier: read journal entries from HEAD, not the working tree"
```

---

## Task 2: Targets, kinds and waivers (§1.2, §1.3, §1.5)

**Files:**
- Modify: `host/research-loop/frontier.py` (new regexes beside `_RUN_ID:165`, new `escalation_targets`)
- Test: `host/tests/test_frontier.py`

- [ ] **Step 1: Write the failing tests**

```python
ESC_TARGET = "evaluate-20260904T090941Z-0a217f40da8f-6446"
OTHER = "evaluate-20260903T101010Z-1111111111aa-1"


def escalation(target, kind="rejection", extra=""):
    return (f"# Retry\n\n**Target run:** {target}\n\n{extra}\n"
            "## NOT RECORDED — the copilot did not agree\n\n"
            f"Escalation kind: {kind}\n\n```\nreason\n```\n")


class EscalationTargets(unittest.TestCase):
    _journal = FrontierJournal._journal          # reuse the fixture helper

    def test_one_rejection_is_counted_with_its_path(self):
        d = self._journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET)})
        targets, unknown = F.escalation_targets(d)
        self.assertEqual(unknown, 0)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)
        self.assertEqual(targets[ESC_TARGET]["latest"],
                         "escalations/20260904T101126Z.md")

    def test_a_file_naming_the_run_three_times_counts_once(self):
        d = self._journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET,
                                      extra=f"{ESC_TARGET} {ESC_TARGET}")})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_a_comparator_in_evidence_is_never_a_target(self):
        d = self._journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, extra=f"vs {OTHER}")})
        targets, _ = F.escalation_targets(d)
        self.assertNotIn(OTHER, targets)

    def test_a_probe_id_target_counts_nothing(self):
        d = self._journal({"escalations/20260904T101126Z.md":
                           escalation(REF_PROBE)})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets, {})

    def test_verifier_failure_does_not_count(self):
        d = self._journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, kind="verifier-failure")})
        targets, unknown = F.escalation_targets(d)
        self.assertEqual(targets, {})
        self.assertEqual(unknown, 0)

    def test_a_missing_kind_line_is_unknown_and_counts_nothing(self):
        # THE HISTORICAL FILES. They predate the kind line and include verifier
        # outages, so defaulting them to `rejection` would retire work nobody
        # judged.
        body = escalation(ESC_TARGET).replace(
            "Escalation kind: rejection\n", "")
        d = self._journal({"escalations/20260904T101126Z.md": body})
        targets, unknown = F.escalation_targets(d)
        self.assertEqual(targets, {})
        self.assertEqual(unknown, 1)

    def test_the_migration_record_classifies_a_historical_file(self):
        body = escalation(ESC_TARGET).replace(
            "Escalation kind: rejection\n", "")
        d = self._journal({
            "escalations/20260904T101126Z.md": body,
            "escalation-kinds.md": ("# classified by hand 2026-09-07\n"
                                    "20260904T101126Z rejection\n")})
        targets, unknown = F.escalation_targets(d)
        self.assertEqual(unknown, 0)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_a_waiver_starts_a_new_episode_rather_than_subtracting(self):
        # THREE rejections, threshold 2, ONE waiver. Subtraction would leave the
        # row retired; windowing makes it selectable again.
        files = {f"escalations/2026090{i}T101126Z.md": escalation(ESC_TARGET)
                 for i in (1, 2, 3)}
        files["waivers/20260905T000000Z.md"] = (
            f"**Waiver:** {ESC_TARGET}\n\nreopening: the tail result is real\n")
        d = self._journal(files)
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets.get(ESC_TARGET, {}).get("rejections", 0), 0)

    def test_a_rejection_after_a_waiver_counts_again(self):
        d = self._journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "waivers/20260905T000000Z.md": f"**Waiver:** {ESC_TARGET}\n",
            "escalations/20260906T101126Z.md": escalation(ESC_TARGET)})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)
        self.assertEqual(targets[ESC_TARGET]["latest"],
                         "escalations/20260906T101126Z.md")

    def test_a_waiver_does_not_make_the_run_recorded(self):
        # The reason waivers live in their own directory: a waiver in `journal/`
        # would cite the id and mark the run written up, defeating itself.
        d = self._journal({"waivers/20260905T000000Z.md":
                           f"**Waiver:** {ESC_TARGET}\n"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_an_unreadable_journal_yields_no_targets(self):
        import tempfile
        d = os.path.join(tempfile.mkdtemp(), "journal")
        os.makedirs(d)
        self.assertEqual(F.escalation_targets(d), ({}, 0))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 tests/test_frontier.py -v -k EscalationTargets`
Expected: FAIL — `module 'frontier' has no attribute 'escalation_targets'`.

- [ ] **Step 3: Add the parsers and `escalation_targets`**

Beside `_RUN_ID` (`frontier.py:165`):

```python
# THE TARGET IS A DECLARED FIELD, NOT PROSE. `_RUN_ID` above is deliberately
# loose, and a rejected entry cites its target, its comparator, the probe behind
# it and the evaluation of it -- so scraping ids would let one rejection retire
# several runs and would read a comparator swap as a new target. Exactly one
# line decides, and only an `evaluate-` id: `index` in `build` maps an id to a
# LIST of rows because one probe can be evaluated twice, so a probe id does not
# identify a row.
_TARGET = re.compile(r"^\*\*Target run:\*\*[ \t]*(\S+)[ \t]*$", re.M)
_KIND = re.compile(r"^Escalation kind:[ \t]*(rejection|verifier-failure)[ \t]*$",
                   re.M)
_WAIVER = re.compile(r"^\*\*Waiver:\*\*[ \t]*(\S+)[ \t]*$", re.M)
_EVAL_ID = re.compile(r"^evaluate-[0-9A-Za-z]+-[0-9a-f]+-\d+$")
_STAMP = re.compile(r"^(\d{8}T\d{6}Z)")
_KIND_LINE = re.compile(r"^(\d{8}T\d{6}Z)[ \t]+(rejection|verifier-failure)$",
                        re.M)


def _stamp_of(name):
    m = _STAMP.match(name)
    return m.group(1) if m else ""


def target_of(text):
    """The declared target of an entry: a canonical evaluation id, or ""."""
    m = _TARGET.search(text or "")
    if not m:
        return ""
    return m.group(1) if _EVAL_ID.match(m.group(1)) else ""
```

Then, after `journaled_run_ids`:

```python
def escalation_targets(journal_dir):
    """`({run id: {rejections, unknown, latest}}, unclassified_total)`.

    ONE ESCALATION FILE IS ONE REJECTION EPISODE, however many times it names
    the run. Only committed files count (§1.1), only a declared `**Target run:**`
    counts (§1.2), only `rejection` counts (§1.3) -- a verifier outage must never
    retire a write-up nobody judged -- and only episodes committed after the
    latest waiver count (§1.5), because subtracting one from three leaves a row
    retired and revives nothing.

    `latest` is carried because the report names the newest qualifying file and
    a bare count cannot produce it.
    """
    esc = _committed_blobs(journal_dir, "escalations")
    waivers = _committed_blobs(journal_dir, "waivers")
    root = _committed_blobs(journal_dir)
    if esc is None:
        return {}, 0

    # Stamp -> kind, for files written before the kind line existed.
    overrides = {}
    for stamp, kind in _KIND_LINE.findall((root or {}).get(
            "escalation-kinds.md", "")):
        overrides[stamp] = kind

    # A waiver opens a new episode: everything at or before its stamp is spent.
    floor = {}
    for name, text in (waivers or {}).items():
        stamp = _stamp_of(name)
        if not stamp:
            continue
        for rid in _WAIVER.findall(text):
            if _EVAL_ID.match(rid) and stamp > floor.get(rid, ""):
                floor[rid] = stamp

    targets, unclassified = {}, 0
    for name in sorted(esc):
        rid = target_of(esc[name])
        if not rid:
            continue
        stamp = _stamp_of(name)
        kind = overrides.get(stamp)
        if not kind:
            m = _KIND.search(esc[name])
            kind = m.group(1) if m else "unknown"
        # ONLY A REAL REJECTION ALLOCATES A RECORD. Allocating for every
        # parseable target left a zero-filled entry for a verifier outage, so
        # "present in the dict" stopped meaning anything. An unclassified
        # escalation is counted in the TOTAL only -- attributing it to the run
        # would imply the write-up was judged, which is the one thing the file
        # does not establish.
        if kind == "unknown":
            unclassified += 1
            continue
        if kind != "rejection":
            continue
        rec = targets.setdefault(rid, {"rejections": 0, "latest": ""})
        if stamp and stamp <= floor.get(rid, ""):
            continue
        rec["rejections"] += 1
        rec["latest"] = f"escalations/{name}"
    return targets, unclassified
```

- [ ] **Step 4: Run the tests**

Run: `python3 tests/test_frontier.py -v`
Expected: PASS. Note `test_a_waiver_starts_a_new_episode` asserts `0`, so an id present with `rejections: 0` is fine — retirement compares against the threshold, not against presence.

- [ ] **Step 5: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/frontier.py \
        tools/queue-forecasting/host/tests/test_frontier.py
git commit -m "frontier: count rejection episodes per declared target run"
```

---

## Task 3: The `handling` tri-state and the missing row fields (§1.3, §1.4)

**Files:**
- Modify: `host/research-loop/frontier.py:383` (`build` signature), `:415` (`row["recorded"]`), `:520` (health), `:563` (`_series_out` rows), `:704` (`main`)
- Test: `host/tests/test_frontier.py`

- [ ] **Step 1: Write the failing tests**

```python
    def test_two_rejections_retire_a_row(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations=esc)
        r = report["series"][0]["rows"][0]
        self.assertEqual(r["handling"], "retired")
        self.assertEqual(r["rejections"], 2)
        self.assertEqual(r["escalation_latest"], "escalations/z.md")
        self.assertEqual(report["health"]["unrecorded_runs"], 0)
        self.assertEqual(report["health"]["retired_runs"], 1)

    def test_one_rejection_does_not_retire(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 1, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations=esc)
        self.assertEqual(report["series"][0]["rows"][0]["handling"],
                         "unrecorded")
        self.assertEqual(report["health"]["unrecorded_runs"], 1)

    def test_recorded_beats_retired(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 9, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, journaled={"eA"},
                         escalations=esc)
        self.assertEqual(report["series"][0]["rows"][0]["handling"], "recorded")

    def test_the_retire_threshold_must_be_at_least_one(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        for bad in ("0", "-1", "x", ""):
            with self.subTest(bad=bad):
                os.environ["QF_FRONTIER_RETIRE_AFTER"] = bad
                try:
                    with self.assertRaises(SystemExit):
                        F.build(rows, EXTRACTS, CONTRACTS)
                finally:
                    del os.environ["QF_FRONTIER_RETIRE_AFTER"]

    def test_the_prereg_fields_reach_the_rows(self):
        note = f"cfg={CFG_REF} cfgh={CFGH_A} bar=p90_miss_tail dir=hold vs=eB tol=0.0011238"
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P,
                    note=note)]
        r = F.build(rows, EXTRACTS, CONTRACTS)["series"][0]["rows"][0]
        self.assertEqual(r["config_digest"], CFGH_A)
        self.assertEqual(r["vs"], "eB")
        self.assertAlmostEqual(r["tol"], 0.0011238)
```

Check the `note=` spelling against `prereg.py:_KEYS` (`cfg cfgh bar dir vs tol ref hyp`) and `prereg.decode` before running; use whatever separator the existing fixtures in this file use for multi-key notes.

- [ ] **Step 2: Run and watch them fail**

Run: `python3 tests/test_frontier.py -v -k retire`
Expected: FAIL — `build() got an unexpected keyword argument 'escalations'`.

- [ ] **Step 3: Add the threshold reader**

Near the top of `frontier.py`, after the regexes:

```python
def retire_after():
    """How many rejection episodes retire a run. Never zero.

    A `0` would retire every unrecorded row the moment it was rejected once --
    in fact on sight -- so an out-of-range or unparseable value is fatal rather
    than a silent fallback. The loop steers on this number.
    """
    raw = os.environ.get("QF_FRONTIER_RETIRE_AFTER", "2")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"QF_FRONTIER_RETIRE_AFTER must be an integer >= 1, got {raw!r}")
    if n < 1:
        raise SystemExit(
            f"QF_FRONTIER_RETIRE_AFTER must be >= 1, got {n}")
    return n
```

- [ ] **Step 4: Thread it through `build`**

`frontier.py:383`:

```python
def build(rows, extracts, contracts, journaled=(), escalations=None,
          problems=None):
```

and replace the `row["recorded"]` assignment (`:415-417`) with:

```python
        row["recorded"] = any(i in journaled
                              for i in (row.get("evaluation"), row.get("probe"))
                              if i)
        # THE STATE THE WHOLE LOOP STEERS ON. Three values, because two could
        # not express "rejected twice, not written up, and not to be picked
        # again" -- which is the livelock of 2026-09-04.
        esc = (escalations or {}).get(row.get("evaluation") or "") or {}
        row["rejections"] = esc.get("rejections", 0)
        row["escalation_latest"] = esc.get("latest", "")
        if row["recorded"]:
            row["handling"] = "recorded"
        elif row["rejections"] >= limit:
            row["handling"] = "retired"
        else:
            row["handling"] = "unrecorded"
```

with `limit = retire_after()` computed once at the top of `build`, before the
loop — so a bad value fails before any work.

- [ ] **Step 5: Project the new fields and counts**

In `_series_out`'s row dict (`frontier.py:563`) add:

```python
                  "handling": r.get("handling", "unrecorded"),
                  "rejections": r.get("rejections", 0),
                  "escalation_latest": r.get("escalation_latest", ""),
                  # PROJECTED BECAUSE THE RETRY TEMPLATE CITES THEM. They were
                  # decoded into `prereg` all along and simply never reached the
                  # JSON, so a retry entry had no citable source for its own
                  # pre-registration -- the exact rejection it must avoid.
                  "config_digest": r["prereg"]["cfgh"],
                  "vs": r["prereg"]["vs"],
                  "tol": r["prereg"]["tol"],
```

In `health` (`frontier.py:520`) replace `unrecorded_runs` and add two:

```python
            "unrecorded_runs": sum(1 for r in rows
                                   if r.get("handling") == "unrecorded"),
            "retired_runs": sum(1 for r in rows
                                if r.get("handling") == "retired"),
            "unclassified_escalations": (problems or {}).get(
                "unclassified", 0),
            "unparseable_waivers": (problems or {}).get(
                "unparseable_waivers", 0),
```

- [ ] **Step 6: Wire `main`**

`frontier.py:704-709`:

```python
    journaled = journaled_run_ids(journal) if journal else set()
    # NO --journal MEANS NOTHING IS RECORDED AND NOTHING IS RETIRED, which makes
    # the report noisy rather than wrong, in both directions.
    escalations, problems = (escalation_targets(journal) if journal
                             else ({}, {}))
    report = build(rows, extracts, contracts, journaled=journaled,
                   escalations=escalations, problems=problems)
```

- [ ] **Step 7: Run everything**

Run: `python3 tests/test_frontier.py -v`
Expected: PASS. `row["recorded"]` is still present and still means the same thing, so no existing assertion changes.

- [ ] **Step 8: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/frontier.py \
        tools/queue-forecasting/host/tests/test_frontier.py
git commit -m "frontier: recorded|unrecorded|retired handling state"
```

---

## Task 4: The report and the prompt stop offering retired rows (§1.4)

**Files:**
- Modify: `host/research-loop/frontier.py:595-670` (`render`)
- Modify: `host/research-loop/tick-prompt.md:17-25`
- Test: `host/tests/test_frontier.py`

- [ ] **Step 1: Write the failing tests**

```python
    def test_a_retired_row_is_named_in_the_report_and_not_offered(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2,
                      "latest": "escalations/20260904T111002Z.md"}}
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS, escalations=esc))
        self.assertIn("RETIRED: eA", text)
        self.assertIn("escalations/20260904T111002Z.md", text)
        self.assertIn("| retired |", text)
        self.assertIn("Unrecorded scored runs: 0.", text)

    def test_unclassified_escalations_are_reported(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS,
                                problems={"unclassified": 3}))
        self.assertIn("UNCLASSIFIED ESCALATIONS: 3", text)

    def test_an_unparseable_waiver_is_reported(self):
        # A MISNAMED WAIVER REVIVES NOTHING, and a waiver is the one file whose
        # whole purpose is operator-initiated revival -- so silence here is the
        # worst place for it.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS,
                                problems={"unparseable_waivers": 1}))
        self.assertIn("UNPARSEABLE WAIVERS: 1", text)
```

- [ ] **Step 2: Run and watch them fail**

Run: `python3 tests/test_frontier.py -v -k report`
Expected: FAIL — the table prints `NO`, and neither new line exists.

- [ ] **Step 3a: Exclude retired rows from the shortlist table**

`render()`'s "needs writing up" table filters on `if r["recorded"]: continue`,
which lists a retired row as `needs writing up: yes`. **That table is the
leader's shortlist** — the surface it actually picks from — so this matters more
than the counter: a retired row here is the livelock again whatever
`unrecorded_runs` says. Change the filter to skip anything whose `handling` is
not `unrecorded`. (Missing from this plan's first draft.)

- [ ] **Step 3: Change the table column**

`frontier.py:667-668`:

```python
            out.append(f"| {row['when']} | {row['config']} |"
                       f" {row['verdict']} | {claimed} | {row['claim']} |"
                       f" {_HANDLING_CELL[row.get('handling', 'unrecorded')]} |")
```

with, beside the other module constants:

```python
# `yes`/`NO` could not say "rejected twice and not to be picked again".
_HANDLING_CELL = {"recorded": "yes", "unrecorded": "NO", "retired": "retired"}
```

- [ ] **Step 4: Add the two report lines**

After the `Unrecorded scored runs:` block (`frontier.py:604-608`):

```python
    if h.get("retired_runs"):
        out.append("")
        for entry in report["series"]:
            for r in entry["rows"]:
                if r.get("handling") != "retired":
                    continue
                out.append(
                    f"RETIRED: {r['evaluation']} —"
                    f" {r['rejections']} rejections, never written up, latest"
                    f" {r['escalation_latest'] or '(unknown)'}."
                    " Not selectable; revive with a committed"
                    " journal/waivers/<stamp>.md carrying"
                    f" `**Waiver:** {r['evaluation']}`.")
    if h.get("unparseable_waivers"):
        out.append("")
        out.append(f"UNPARSEABLE WAIVERS: {h['unparseable_waivers']} file(s)"
                   " in journal/waivers/ have no `<stamp>` filename prefix and"
                   " revive NOTHING. Rename them"
                   " `YYYYMMDDTHHMMSSZ.md` and commit.")
    if h.get("unclassified_escalations"):
        out.append("")
        out.append(f"UNCLASSIFIED ESCALATIONS:"
                   f" {h['unclassified_escalations']} escalation(s) carry no"
                   " `Escalation kind:` line and are NOT counted toward"
                   " retirement. Classify them in"
                   " journal/escalation-kinds.md.")
```

- [ ] **Step 5: Exclude retired rows from the prompt's actions**

`tick-prompt.md`, action 1 (`:17-21`) and action 2 (`:22-25`) — replace both with:

```markdown
1. **A finished run is unrecorded.** The frontier's `written up` column says
   `NO`. Write it up. Stop. (A run counts as written up once a RECORDED journal
   entry cites its run id — so cite the id, or the next tick will do this again.
   An escalated entry does not count, because it was rejected.)
2. **A pre-registered claim came out false.** The frontier shows `broken` and
   `written up: NO`. Write what that rules out. A refuted hypothesis is a
   result, and the copilot is told to accept refutations readily. Stop.

**`retired` is not `NO`.** A row whose `written up` column says `retired` was
written up twice and rejected twice, and is deliberately no longer available:
actions 1 and 2 must skip it. It is listed under `RETIRED:` so a
human can see what was given up on, and only a human can bring it back. Picking
it anyway is how three ticks were spent on one run on 2026-09-04.
```

- [ ] **Step 6: Run**

Run: `python3 tests/test_frontier.py -v`
Expected: PASS.

- [ ] **Step 7: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/frontier.py \
        tools/queue-forecasting/host/research-loop/tick-prompt.md \
        tools/queue-forecasting/host/tests/test_frontier.py
git commit -m "frontier: surface retirement in the report and the prompt"
```

---

## Task 5: The entry template declares its target (§1.2)

**Files:**
- Modify: `host/research-loop/tick-prompt.md:156-170`
- Modify: `host/research-loop/verify-prompt.md` (one paragraph)

- [ ] **Step 1: Add the field to the template**

In `tick-prompt.md`'s `## Your output` block, make `**Target run:**` the first
field:

```markdown
# <one-line title>

**Target run:** <the `evaluate-` id this entry is ABOUT, or the literal `none`>

**Action taken:** <which of the six, and the exact command you ran>

**Claim:** <what you assert, with the numbers it rests on and the run ids>

**Confidence and what would change it:** <the honest version>

**Not concluded:** <what a reader might wrongly infer from this, spelled out>

**Evidence:** <for every figure NOT in the frontier JSON: the exact command and
the relevant lines of its output, pasted>
```

and below it:

```markdown
**`Target run:` is machine-read, so it has exactly one job.** It is the single
`evaluate-` id this entry is about — the row you are writing up, or the
evaluation of the run you just submitted. Never a `probe-` id: one probe can be
evaluated under two contracts, so a probe id does not identify a row. Never a
comparator, however central it is to the argument; cite that in `Claim:` like
any other id. If this tick wrote up no run — a waiting entry — the answer is
`none`, and that is a perfectly good answer.

Getting it wrong is not a rejection, it is worse: the loop uses this field to
tell "the leader is being rewritten on the same run" from "the leader is drifting
across different ones", and an id that names nothing is read as the second.
```

- [ ] **Step 2: Tell the copilot it is not a figure**

In `verify-prompt.md`, in `## What is not yours to reject`, add:

```markdown
- **`Target run:` is not a claim.** It is a machine-read field the loop uses to
  identify which row the entry is about. It is not a figure, it is not evidence,
  and its presence, absence or value is never a reason to reject.
```

- [ ] **Step 3: Verify the prompts still assemble**

Run: `./tick.sh --dry-run 2>&1 | tail -40`
Expected: the leader context prints, containing `**Target run:**` in the output
template and the `retired is not NO` paragraph. (`--dry-run` invokes no agent.)

- [ ] **Step 4: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/tick-prompt.md \
        tools/queue-forecasting/host/research-loop/verify-prompt.md
git commit -m "prompts: require a machine-readable Target run field"
```

---

> **Sequencing constraint, found by review of Task 5.** Task 5's prompt text
> tells the leader that the loop distinguishes "the same run is being rewritten"
> from "the agent is drifting across different runs". That is true only once
> Task 6 lands — `set_target`, `handling_of` and `suppressible` do not exist in
> `tick.sh` before it, and today's `consecutive-disagreements` is a flat counter
> that treats three rejections of one run exactly like three rejections of
> three. **Tasks 5 and 6 must not be committed separately**, or the prompt
> describes a capability the loop does not have.

## Task 6: Two counters and the usable verdict (§2.1, §2.2, §2.3)

**Files:**
- Modify: `host/research-loop/tick.sh:54-58` (knobs), `:755-765` (verdict parsing), `:767-830` (the streak block)
- Test: `host/tests/test_tick.sh`

- [ ] **Step 1: Read how `test_tick.sh` isolates the script**

Run: `sed -n '1,80p' tests/test_tick.sh`

It sources selected functions out of `tick.sh` the same way `test_unit_drift.sh`
sources `unit_matches` out of `phase2-setup.sh`. Add the new helpers in a shape
that extraction can reach: top-level `name() { ... }`, no subshell wrapper.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_tick.sh`, following the file's existing `ok`/`bad` helper
convention:

```bash
# --- target helpers -------------------------------------------------------
T="$TMP/state/last-reject-target"
EVAL_ID=evaluate-20260904T090941Z-0a217f40da8f-6446

set_target "$T" "$EVAL_ID" && [ "$(target "$T")" = "$EVAL_ID" ] \
  && ok "set_target/target round-trip a canonical evaluation id" \
  || bad "set_target/target round-trip a canonical evaluation id"

set_target "$T" none && [ "$(target "$T")" = none ] \
  && ok "none round-trips" || bad "none round-trips"

printf 'probe-20260830T202842Z-4a2ae967d664-5418\n' >"$T"
target "$T" && bad "a probe id must not validate" || ok "a probe id must not validate"

printf '%s\n%s\n' "$EVAL_ID" "$EVAL_ID" >"$T"
target "$T" && bad "a multi-line target must not validate" \
             || ok "a multi-line target must not validate"

printf 'not-an-id\n' >"$T"
target "$T" && bad "a malformed target must not validate" \
             || ok "a malformed target must not validate"

# --- the suppressible predicate ------------------------------------------
# handling_of reads the frontier JSON the tick just built. `ambiguous` is two
# rows sharing an evaluation id; `absent` is an id the scoreboard no longer has.
cat >"$TMP/f.json" <<JSON
{"series":[{"rows":[
  {"evaluation":"$EVAL_ID","handling":"unrecorded"},
  {"evaluation":"eRec","handling":"recorded"},
  {"evaluation":"eRet","handling":"retired"},
  {"evaluation":"eTwice","handling":"unrecorded"},
  {"evaluation":"eTwice","handling":"unrecorded"}]}]}
JSON
[ "$(handling_of "$TMP/f.json" "$EVAL_ID")" = unrecorded ] \
  && ok "an unrecorded row resolves" || bad "an unrecorded row resolves"
[ "$(handling_of "$TMP/f.json" eRec)" = recorded ] \
  && ok "a recorded row resolves" || bad "a recorded row resolves"
[ "$(handling_of "$TMP/f.json" eRet)" = retired ] \
  && ok "a retired row resolves" || bad "a retired row resolves"
[ "$(handling_of "$TMP/f.json" eTwice)" = ambiguous ] \
  && ok "two rows one id is ambiguous" || bad "two rows one id is ambiguous"
[ "$(handling_of "$TMP/f.json" eGone)" = absent ] \
  && ok "an id off the scoreboard is absent" || bad "an id off the scoreboard is absent"

for state in recorded retired ambiguous absent; do
  suppressible "$TMP/f.json" "$state" && bad "$state must not suppress" \
    || ok "$state must not suppress"
done
suppressible "$TMP/f.json" unrecorded && ok "unrecorded suppresses" \
  || bad "unrecorded suppresses"
```

Then the streak-transition cases, driven through the same harness the file
already uses to run a whole tick with stubbed `claude` and `codex`:

```bash
# A repeat rejection of the SAME unrecorded target does not advance drift.
# A rejection of a DIFFERENT one does. `none` always does.
# A non-zero copilot exit carrying `VERDICT: AGREE` is verifier-failure:
#   it advances the verifier counter, does NOT reset it, does NOT advance
#   drift, and stores NO target.
# An exit-0 copilot printing prose with no anchored line is the same.
# A usable DISAGREE resets the verifier counter.
```

Write one case per line above, asserting on the two counter files and on
`last-reject-target` after each simulated tick.

- [ ] **Step 3: Run and watch them fail**

Run: `./tests/test_tick.sh 2>&1 | grep -E 'FAIL|not found'`
Expected: FAIL — `set_target: command not found`, `handling_of: command not
found`, `suppressible: command not found`.

- [ ] **Step 4: Add the knobs**

`tick.sh:54-58`:

```bash
MAX_DISAGREE="${QF_TICK_MAX_DISAGREE:-3}"
MAX_VERIFIER_FAILS="${QF_TICK_MAX_VERIFIER_FAILS:-3}"
```

and add `MAX_VERIFIER_FAILS` to the numeric-knob validation loop at `:120-121`.

- [ ] **Step 5: Add the three helpers beside `counter`/`set_counter`**

```bash
# THE TARGET CANNOT USE `counter`. That helper refuses any value containing a
# non-digit (by design -- a counter that cannot be trusted is a stop), so it
# fails on every run id. Two shapes are legal and nothing else: the literal
# `none`, or one canonical EVALUATION id. A probe id is refused because one
# probe can be evaluated under two contracts, so it does not identify a row.
target() {  # target <path> -- prints the value, or fails
  local path="$1" value
  [ -e "$path" ] || { echo none; return 0; }
  value="$(cat "$path" 2>/dev/null)" || return 1
  case "$value" in
    none) echo none; return 0 ;;
    evaluate-*) ;;
    *) return 1 ;;
  esac
  printf '%s' "$value" \
    | grep -qxE 'evaluate-[0-9A-Za-z]+-[0-9a-f]+-[0-9]+' || return 1
  echo "$value"
}

set_target() {  # set_target <path> <value> -- persists, or fails
  local path="$1" value="$2"
  printf '%s\n' "$value" >"$path.tmp" || return 1
  mv "$path.tmp" "$path" || return 1
}

# WHAT THE FRONTIER SAYS ABOUT ONE ID: recorded | unrecorded | retired |
# ambiguous | absent. `ambiguous` and `absent` exist because a target that does
# not resolve to exactly one row is one retirement cannot bound -- see
# `suppressible`.
handling_of() {  # handling_of <frontier.json> <run id>
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    report = json.load(open(sys.argv[1]))
except Exception:
    print("absent"); raise SystemExit(0)
rid = sys.argv[2]
hits = [r for s in report.get("series", []) for r in s.get("rows", [])
        if r.get("evaluation") == rid]
print("absent" if not hits else
      "ambiguous" if len(hits) > 1 else
      hits[0].get("handling", "unrecorded"))
PY
}

# THE LOAD-BEARING PREDICATE (design §2.1). Suppressing the drift streak for a
# repeated target is only safe because RETIREMENT bounds it, and retirement only
# counts episodes against a resolved, unrecorded row. Suppressing on anything
# else -- ambiguous, absent, recorded, retired -- is unbounded, and rebuilds the
# 2026-09-04 livelock with the drift brake switched off.
suppressible() {  # suppressible <frontier.json> <handling>
  [ "$2" = unrecorded ]
}
```

- [ ] **Step 6: Make the no-verdict case a verifier failure**

`tick.sh:762-765` — the fallback currently leaves `VERIFIER_FAILED` at 0:

```bash
  # NO USABLE VERDICT IS AN INFRASTRUCTURE FAILURE, NOT A DISAGREEMENT.
  # A usable verdict is `exit 0 AND a valid anchored final VERDICT line`; a
  # copilot that returned prose without one verified nothing, exactly like one
  # that could not start. It still escalates (nothing may be recorded
  # unverified), but it advances the verifier counter rather than accusing the
  # leader of drifting.
  if [ -z "$VERDICT" ]; then
    VERDICT="DISAGREE"
    VERIFIER_FAILED=1
    REASON="no VERDICT line in the copilot's reply
$REASON"
  fi
```

The non-zero-exit branch at `:741-751` already sets `VERIFIER_FAILED=1` and
already refuses to parse the output — leave both exactly as they are, and add
one line to that comment: *"and this is the other half of the usable-verdict
definition: verdict text from a process that exited non-zero is not a verdict."*

- [ ] **Step 7: Rewrite the streak block**

Replace `tick.sh:767-830` (from `STAMP="$(date ...)"` to the end of the
`if [ "$N" -ge "$MAX_DISAGREE" ]` block):

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DISAGREE_FILE="$STATE/consecutive-disagreements"
VERIFIER_FILE="$STATE/consecutive-verifier-failures"
TARGET_FILE="$STATE/last-reject-target"
VERIFIER_FAILED="${VERIFIER_FAILED:-0}"

# THE ENTRY'S DECLARED TARGET, and its state in the frontier THIS tick built.
# THE FRONTIER THIS TICK TRUSTS -- which `EVIDENCE` already is, so do NOT
# re-derive it. `frontier2.json` is built after the leader acted (it is what the
# copilot was shown), so a run this tick submitted and scored appears there and
# not in the pre-leader snapshot: judging the target against the stale snapshot
# would read every fresh submission as `absent` and advance the drift brake on
# exactly the ticks that did the most work. But `EVIDENCE` is set by the full
# `frontier()` contract (both renders succeeded, the JSON is non-empty), while a
# bare `[ -s frontier2.json ]` is not -- a `--json` pass that writes complete
# output and then exits non-zero makes the two disagree, and the tick would judge
# suppression against a snapshot it had just declared unusable.
FRONTIER_NOW="$EVIDENCE"
ENTRY_TARGET="$(sed -n 's/^\*\*Target run:\*\*[ \t]*\([^ \t]*\)[ \t]*$/\1/p' \
                "$PENDING" 2>/dev/null | head -1)"
case "$ENTRY_TARGET" in
  evaluate-*) ENTRY_HANDLING="$(handling_of "$FRONTIER_NOW" "$ENTRY_TARGET")" ;;
  *) ENTRY_TARGET=none; ENTRY_HANDLING=absent ;;
esac

if [ "$VERDICT" = "AGREE" ]; then
  mv "$PENDING" "$JOURNAL/$STAMP.md"
  RECORDED="$JOURNAL/$STAMP.md"
  # A FAILURE HERE IS NOT FATAL, unlike the increment: an un-reset streak is
  # conservative (it pauses sooner), while an un-incremented one is not.
  set_counter "$DISAGREE_FILE" 0 \
    || say "note: could not reset the disagreement counter"
  set_counter "$VERIFIER_FILE" 0 \
    || say "note: could not reset the verifier-failure counter"
  # CLEARED ON AGREE, which is what was missing: without this the next
  # rejection of a DIFFERENT target could read as a retry of this one.
  set_target "$TARGET_FILE" none \
    || say "note: could not clear the last rejected target"
  say "verified; recording $STAMP.md"
else
  RECORDED="$JOURNAL/escalations/$STAMP.md"
  {
    cat "$PENDING"
    echo
    echo "---"
    echo
    echo "## NOT RECORDED — the copilot did not agree"
    echo
    # MACHINE-READ BY `escalation_targets`. `verifier-failure` must never count
    # toward retirement: two `codex` outages would otherwise retire a write-up
    # nobody ever judged.
    if [ "$VERIFIER_FAILED" = 1 ]; then
      echo "Escalation kind: verifier-failure"
    else
      echo "Escalation kind: rejection"
    fi
    echo
    echo "This entry is an escalation, not a finding. The claim above was not"
    echo "accepted and must not be cited as a result."
    echo
    echo '```'
    printf '%s\n' "$REASON"
    echo '```'
  } >"$RECORDED"
  rm -f "$PENDING"

  if [ "$VERIFIER_FAILED" = 1 ]; then
    # INFRASTRUCTURE. The retry loop above absorbed the transient case; what is
    # left is a copilot that is DOWN, and a loop that cannot verify anything must
    # not keep spending a leader turn an hour -- twelve a day, recording nothing.
    # NOTHING IS STORED AS A TARGET: the entry was never judged, so the next tick
    # must be free to write it up normally rather than under the reduced retry
    # template.
    if PREV="$(counter "$VERIFIER_FILE")" \
       && set_counter "$VERIFIER_FILE" "$((PREV + 1))"; then
      VN=$((PREV + 1))
    else
      VN="$MAX_VERIFIER_FAILS"
      say "WARNING the verifier-failure counter at $VERIFIER_FILE cannot be"
      say "  persisted, so the streak cannot be tracked. Pausing now."
    fi
    say "the copilot did not run ($VN consecutive); escalated to escalations/$STAMP.md"
    say "  this streak is infrastructure, not drift"
    if [ "$VN" -ge "$MAX_VERIFIER_FAILS" ]; then
      pause_now "consecutive-verifier-failures" "$VN" "$RECORDED" "$STAMP"
    fi
  else
    # RESEARCH DRIFT, counted once per rejected target EPISODE. A repeat of the
    # same resolved, unrecorded target is bounded by retirement (design §1.3),
    # so it does not advance; anything that retirement cannot reach does.
    STORED="$(target "$TARGET_FILE")" || STORED=""
    if suppressible "$FRONTIER_NOW" "$ENTRY_HANDLING" \
       && [ -n "$STORED" ] && [ "$STORED" = "$ENTRY_TARGET" ]; then
      if ! N="$(counter "$DISAGREE_FILE")"; then
        # FAIL CLOSED, BUT SAY SO. Silently substituting the threshold pauses
        # the loop reporting a drift streak when the real cause was a counter
        # that could not be read -- the escalation must name which happened.
        N="$MAX_DISAGREE"
        say "WARNING the disagreement counter at $DISAGREE_FILE cannot be"
        say "  read, so the streak cannot be tracked. Pausing now."
      fi
      say "NOT verified; same target as last tick ($ENTRY_TARGET)"
      say "  the drift streak stays at $N -- retirement bounds this, not the brake"
    else
      if PREV="$(counter "$DISAGREE_FILE")" \
         && set_counter "$DISAGREE_FILE" "$((PREV + 1))"; then
        N=$((PREV + 1))
      else
        N="$MAX_DISAGREE"
        say "WARNING the disagreement counter at $DISAGREE_FILE cannot be"
        say "  persisted, so the streak cannot be tracked. Pausing now."
      fi
      # A TARGET IS STORED ONLY IF SUPPRESSION COULD LEGITIMATELY APPLY TO IT
      # NEXT TIME. Storing an ambiguous, absent, recorded or retired id would
      # suppress a streak retirement cannot bound.
      if suppressible "$FRONTIER_NOW" "$ENTRY_HANDLING"; then
        set_target "$TARGET_FILE" "$ENTRY_TARGET" \
          || { say "WARNING cannot persist the rejected target; treating the"
               say "  next rejection as a new one"; }
      else
        set_target "$TARGET_FILE" none || true
        say "  target $ENTRY_TARGET is $ENTRY_HANDLING: not suppressible"
      fi
      say "NOT verified ($N consecutive); escalated to escalations/$STAMP.md"
    fi
    if [ "$N" -ge "$MAX_DISAGREE" ]; then
      pause_now "consecutive-disagreements" "$N" "$RECORDED" "$STAMP"
    fi
  fi
fi
```

- [ ] **Step 8: Add `pause_now`, shared by both brakes**

Beside the other helpers:

```bash
# WRITING THE BRAKE, AND RAISING THE ALARM. Two callers, one behaviour, and the
# brake name reaches both the PAUSE file and the issue title so a human can tell
# an outage from drift without opening anything.
pause_now() {  # pause_now <brake> <n> <escalation path> <stamp>
  local brake="$1" n="$2" recorded="$3" stamp="$4"
  # THE BRAKE NAME VERBATIM, because a human greps for it -- but only once in
  # the sentence: an earlier draft rendered "3 consecutive
  # consecutive-disagreements".
  if ! printf 'auto-paused %s: %s reached %s\nsee %s\nstamp: %s\n' \
       "$stamp" "$brake" "$n" "$recorded" "$stamp" >"$QF_RESEARCH/PAUSE"; then
    # The PAUSE file IS the brake. If it cannot be written, say so as loudly as
    # possible rather than reporting a pause that did not happen.
    say "CRITICAL cannot write $QF_RESEARCH/PAUSE. The loop is NOT paused."
    say "  Disable the timer by hand: sudo systemctl disable --now qf-tick.timer"
    return 1
  fi
  say "PAUSED: $n consecutive $brake"
  "$HERE/pause-issue.sh" open "$QF_RESEARCH/PAUSE" "$brake" "$n" "$recorded" \
    || say "WARNING could not file the pause issue; the loop is still paused"
}
```

- [ ] **Step 9: Run**

Run: `./tests/test_tick.sh`
Expected: every case passes, including the pre-existing ones. `pause-issue.sh`
does not exist yet, so `pause_now` logs the WARNING — assert that, and Task 8
flips it.

- [ ] **Step 10: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/tick.sh \
        tools/queue-forecasting/host/tests/test_tick.sh
git commit -m "tick: separate research drift from verifier outage"
```

---

## Task 7: Retry mode, decided before the leader runs (§3.1, §3.2)

**Files:**
- Modify: `host/research-loop/tick.sh` (after the frontier is built, before the leader context is assembled)
- Modify: `host/research-loop/tick-prompt.md`
- Test: `host/tests/test_tick.sh`

- [ ] **Step 1: Write the failing tests**

```bash
# RETRY MODE IS DECIDED FROM STATE + THE FRESH FRONTIER, never from PENDING.md,
# because the instruction has to be in the leader's context before it writes.
set_target "$T" "$EVAL_ID"
retry_target "$TMP/f.json" "$T" >"$TMP/out" \
  && [ "$(cat "$TMP/out")" = "$EVAL_ID" ] \
  && ok "an unrecorded stored target is the retry target" \
  || bad "an unrecorded stored target is the retry target"

for id in eRec eRet eTwice eGone; do
  set_target "$T" "$id" 2>/dev/null || printf '%s\n' "$id" >"$T"
  retry_target "$TMP/f.json" "$T" >/dev/null \
    && bad "$id must not enter retry mode" \
    || ok "$id must not enter retry mode"
  [ "$(cat "$T")" = none ] \
    && ok "$id is cleared from the target file" \
    || bad "$id is cleared from the target file"
done

set_target "$T" none
retry_target "$TMP/f.json" "$T" >/dev/null \
  && bad "none must not enter retry mode" || ok "none must not enter retry mode"
```

Plus one whole-tick case: with a retry target set, the assembled leader context
contains `## This tick is a RETRY`; with `none`, it does not.

- [ ] **Step 2: Run and watch them fail**

Run: `./tests/test_tick.sh 2>&1 | grep -E 'FAIL|not found'`
Expected: `retry_target: command not found`.

- [ ] **Step 3: Add `retry_target`**

```bash
# THE RETRY DECISION, made from the stored target and the frontier THIS tick
# built -- never from the entry, which does not exist yet. A stored target that
# has since been recorded by another route, retired, gone ambiguous or left the
# scoreboard is cleared here, so the next rejection is counted as new.
retry_target() {  # retry_target <frontier.json> <target file> -- prints id, or fails
  # THE SAME PREDICATE THE STREAK USED (design §2.1), deliberately: if these two
  # ever disagreed the tick could suppress a streak for a target it then refuses
  # to instruct the leader to retry -- a rejection that costs a tick and teaches
  # the loop nothing.
  local json="$1" file="$2" stored state
  stored="$(target "$file")" || { set_target "$file" none || true; return 1; }
  [ "$stored" != none ] || return 1
  state="$(handling_of "$json" "$stored")"
  # `suppressible <handling>` -- it takes the RESOLVED state, not the path;
  # `handling_of` is the one thing that resolves an id, and it did so above.
  if suppressible "$state"; then
    printf '%s\n' "$stored"
    return 0
  fi
  set_target "$file" none || true
  say "the stored retry target $stored is $state; cleared"
  return 1
}
```

- [ ] **Step 4: Inject the retry block into the leader context**

In the context assembly (`tick.sh:472-500`), immediately after the
`prev-escalation.md` block:

```bash
  # RETRY MODE, and the action is NOT open in it. The shrink instruction only
  # means something if the tick also says which target it applies to: deriving
  # the retry from whatever the leader happens to pick cannot work, because the
  # instruction has to be read before the choice is made.
  if RETRY_ID="$(retry_target "$CTX/frontier.json" "$STATE/last-reject-target")"; then
    cat "$HERE/retry-prompt.md"
    echo
    echo "The target of this retry is \`$RETRY_ID\`."
    echo
  fi
```

- [ ] **Step 5: Write `retry-prompt.md`**

Create `host/research-loop/retry-prompt.md`:

```markdown
## This tick is a RETRY, and the action is not open

Your last entry was not recorded. This tick is **action 1 on one named target**
and the other five actions are unavailable: do not submit an experiment, do not
pick a different row, do not write up anything else.

**Rewrite by shrinking, not by rewriting.** On 2026-09-04 a retry fixed the
defect it was told about and introduced a new one — the round-2 entry added a
superlative that was false. Every hedge, aside and piece of context is another
sentence that can be wrong, and the finding is lost when any of them is. So the
entry is the pre-registered claim and the evidence for it, in exactly this
shape:

```markdown
# Retry: <config>@<config_digest>, <target run id>

**Target run:** <the target named above>

**Action taken:** 1 — retry of <the escalation named in the feedback block>

**Claim:** <the row's `claim`, `bar` and `direction` verbatim from the row
pasted below, and the figures they rest on. Signed deltas at the precision the JSON
supplies. No other config, no other metric.>

**Confidence and what would change it:** <one sentence: the row's `tol`, and
this series' `holdout` window.>

**Not concluded:** <one sentence: what one cohort does not establish.>

**Evidence:** <pasted command output for any figure not in the JSON. Nothing
else.>
```

Four bans, each of which has already cost a finding:

1. **No rounding-equivalence.** "Unchanged to four decimals" is a figure claim,
   and it was false: the values were 0.0817 and 0.0818. State the signed delta.
2. **No superlatives and no firsts** unless you paste the JSON rows that
   establish the comparison. "The first quantile config to clear the tail bar"
   was rejected because `val7_nop90` had already reached 0.286152780.
3. **The title names the action and the config, nothing else.** It never says
   whether a metric got better or worse — a −1.43pp MAE move is not an
   "improvement".
4. **No comparison to any config except the pre-registered `vs`.**

**THE LEADER DOES NOT GET `frontier.json`** — it gets `frontier.md`, whose row
table carries no `config_digest`, no `vs`, no `tol`, no `escalation_latest` and
no per-row metric (only a `%.4g` cell for the winning config); the JSON goes to
the copilot alone. Verified by rendering a real row: all four absent. So the
injection **pastes the target row's JSON object** into the context, at full
precision, right after the target id, and the template cites that. A template
that says "from the JSON" asks for a blank field or a remembered number, which
is the rejection the retry exists to prevent. There is no independent-cohort figure
for a row that missed a bar, and you must not supply one: use the `holdout`
window instead.
```

- [ ] **Step 6: Run**

Run: `./tests/test_tick.sh` and `./tick.sh --dry-run`
Expected: tests pass; the dry run shows no retry block with a `none` target, and
shows it with a target set by hand into the state directory.

- [ ] **Step 7: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/tick.sh \
        tools/queue-forecasting/host/research-loop/retry-prompt.md \
        tools/queue-forecasting/host/tests/test_tick.sh
git commit -m "tick: retry mode with a reduced entry template"
```

---

## Task 8: `pause-issue.sh` (§4.1–§4.4)

**Files:**
- Create: `host/research-loop/pause-issue.sh`, `host/research-loop/pause-resume-allowlist.txt`
- Create: `host/tests/test_pause_issue.sh`
- Modify: `host/research-loop/tick.sh:160-164` and `:204-205`

- [ ] **Step 1: Write the allowlist**

`host/research-loop/pause-resume-allowlist.txt`:

```
# GitHub logins permitted to release an auto-PAUSE by closing its issue.
# NORMAL-WORKFLOW POLICY, NOT ENFORCEMENT: `research` can already remove the
# PAUSE file, so this is about who is expected to, not who is able to. One login
# per line; `#` comments and blank lines ignored.
lotas
```

- [ ] **Step 2: Write `test_pause_issue.sh` first**

Create `host/tests/test_pause_issue.sh` with a `gh` stub on `PATH` whose replies
come from files the test writes, one case per row of design §4.2:

```bash
#!/usr/bin/env bash
# `pause-issue.sh` against a stubbed `gh`. Every case is a row of design §4.2.
#
# WHY A STUB AND NOT A RECORDING. The two answers that matter are the ones
# GitHub will not give you on demand: an API error, and a closed issue with no
# `closed` event. Both must leave the loop paused, and neither is reachable from
# a fixture repository.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../research-loop/pause-issue.sh"
pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/ws"
export PATH="$TMP/bin:$PATH"
export QF_PAUSE_ISSUE_REPO=lotas/qf-research
export QF_PAUSE_TOKEN_FILE="$TMP/token"; echo dummy >"$TMP/token"
export QF_PAUSE_ALLOWLIST="$HERE/../research-loop/pause-resume-allowlist.txt"

# The stub answers from $TMP/gh-<verb>.json and records its argv.
cat >"$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_LOG"
case "$*" in
  *"issues/"*"/events"*) f="$GH_DIR/events.json" ;;
  *"issues/"*"/comments"*) f="$GH_DIR/comments.json" ;;
  *"issue view"*|*"issues/"*) f="$GH_DIR/issue.json" ;;
  *"issue list"*|*"search/issues"*) f="$GH_DIR/search.json" ;;
  *"issue create"*) f="$GH_DIR/create.json" ;;
  *"label"*) f="$GH_DIR/label.json" ;;
  *) f=/dev/null ;;
esac
[ -f "$f.rc" ] && exit "$(cat "$f.rc")"
[ -f "$f" ] && cat "$f"
exit 0
STUB
chmod +x "$TMP/bin/gh"
export GH_DIR="$TMP/gh" GH_LOG="$TMP/gh.log"; mkdir -p "$GH_DIR"
```

Then the cases — each sets up `$GH_DIR/*.json`, runs
`"$SCRIPT" check "$TMP/ws/PAUSE"`, and asserts on the exit status, on whether
`PAUSE` still exists, and on the counters:

| Case | Fixture | Assert |
|---|---|---|
| no PAUSE | file absent | exit 0, "not paused" |
| PAUSE without `issue:` | one-line PAUSE | exit non-zero, PAUSE intact, `issue create` in `gh.log` |
| repo mismatch | `issue: other/repo#4` | PAUSE intact, "mismatch" logged, no resume |
| issue open | `issue.json` `"state":"open"` | PAUSE intact |
| closed by allowlisted login | `events.json` with `[{"event":"closed","actor":{"login":"lotas"}}]` | PAUSE gone, both counters `0`, target `none` |
| closed by someone else | actor `"drifter"` | PAUSE intact, "drifter" logged |
| closed, no `closed` event | `events.json` `[]` | PAUSE intact |
| API error | `events.json.rc` = `1` | PAUSE intact |
| two `closed` events | two entries, latest `lotas` | resumes on the latest |
| idempotent open, closed match | `search.json` returns a closed issue carrying the stamp | no second `issue create` in `gh.log` |
| label creation fails | `label.json.rc` = `1` | `issue create` still ran |
| own-login comment | `comments.json` author = token login | comment not in the directive file |

- [ ] **Step 3: Run and watch it fail**

Run: `./tests/test_pause_issue.sh`
Expected: FAIL — the script does not exist.

- [ ] **Step 4: Write `pause-issue.sh`**

```bash
#!/usr/bin/env bash
# The pause issue: the alarm, and the handle a human uses to release the brake.
#
# WHAT THIS IS NOT. It is not enforcement. `research` can remove the PAUSE file
# outright, and the token below can close the issue it filed, so nothing here
# prevents a self-release -- see design §4 and §4.5. What it buys is that a
# pause REACHES someone within the hour instead of in two and a half days, and
# that a release is an attributable, timestamped GitHub event rather than an
# `rm`.
#
#   pause-issue.sh open  <pause file> <brake> <n> <escalation path>
#   pause-issue.sh check <pause file>
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${QF_PAUSE_ISSUE_REPO:-}"
TOKEN_FILE="${QF_PAUSE_TOKEN_FILE:-$HOME/.config/qf/pause-issue-token}"
ALLOWLIST="${QF_PAUSE_ALLOWLIST:-$HERE/pause-resume-allowlist.txt}"
STATE="${QF_TICK_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/qf-tick}"
LABEL="qf-pause"
say() { printf '[pause-issue %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# PINNED IN TRUSTED CONFIGURATION, never derived from `git remote`: the remote
# lives in a research-writable checkout, and the repository this talks to must
# not be something the research identity can redirect.
[ -n "$REPO" ] || { say "QF_PAUSE_ISSUE_REPO is unset; filing nothing"; exit 1; }

gh_() {  # gh with only the pause token in scope
  local token=""
  [ -r "$TOKEN_FILE" ] && token="$(cat "$TOKEN_FILE" 2>/dev/null)"
  [ -n "$token" ] || { say "no token at $TOKEN_FILE"; return 1; }
  GH_TOKEN="$token" GITHUB_TOKEN="$token" gh "$@"
}

allowed() {  # allowed <login>
  local login="$1"
  [ -n "$login" ] || return 1
  [ -r "$ALLOWLIST" ] || { say "no allowlist at $ALLOWLIST"; return 1; }
  grep -vE '^[[:space:]]*(#|$)' "$ALLOWLIST" | grep -qxF "$login"
}

cmd_open() {
  local pause="$1" brake="$2" n="$3" recorded="$4"
  local stamp; stamp="$(sed -n 's/^stamp: //p' "$pause" | head -1)"
  [ -n "$stamp" ] || { say "no stamp in $pause"; return 1; }

  # THE LABEL IS PREFLIGHTED AND ALLOWED TO FAIL. An alarm is worth more than a
  # label, so a failure here is logged and ignored -- which is also why the
  # label can never be part of the idempotency key below.
  local label_ok=1
  gh_ label create "$LABEL" --repo "$REPO" \
      --description "auto-paused research loop" --color B60205 \
      >/dev/null 2>&1 || label_ok=0

  # IDEMPOTENT ON THE STAMP, ACROSS OPEN AND CLOSED. If creation succeeded and
  # this process died before rewriting PAUSE, the retry runs after a human may
  # already have closed the issue -- and an open-only search would then file a
  # duplicate against a pause that was already handled.
  local found
  found="$(gh_ issue list --repo "$REPO" --state all --search "$stamp" \
             --json number,body --jq \
             ".[] | select(.body | contains(\"$stamp\")) | .number" \
           2>/dev/null | head -1)"
  if [ -n "$found" ]; then
    say "issue $REPO#$found already carries $stamp"
  else
    local body
    body="$(cmd_body "$brake" "$n" "$recorded" "$stamp")"
    local args=(--repo "$REPO" --title "PAUSED $stamp: $brake at $n" --body "$body")
    [ "$label_ok" = 1 ] && args+=(--label "$LABEL")
    local url
    url="$(gh_ issue create "${args[@]}" 2>/dev/null)" || {
      say "could not create the issue"; return 1; }
    found="${url##*/}"
    say "filed $REPO#$found"
  fi

  # BIND THE AUTHORIZATION TO ONE ISSUE: repo, number and stamp all have to
  # agree in `check`, so a bare mutable number is not by itself a release.
  printf 'issue: %s#%s\n' "$REPO" "$found" >>"$pause"
}

cmd_body() {
  local brake="$1" n="$2" recorded="$3" stamp="$4"
  printf '%s\n' \
    "The research loop auto-paused at \`$stamp\`." \
    "" \
    "- brake: \`$brake\` reached $n" \
    "- escalation: \`$recorded\`" \
    "" \
    "## To release it" \
    "" \
    "**Comment first if you want to steer the next tick**, then **close this" \
    "issue**. The next tick reads the closing actor, and resumes only if that" \
    "login is in \`pause-resume-allowlist.txt\`. Comments from allowlisted" \
    "logins are handed to the leader as a directive — labelled as instruction," \
    "not as evidence, and never shown to the verifier." \
    "" \
    "Closing without commenting is a plain resume." \
    "" \
    "## The last three rejections" \
    ""
  local d; d="$(dirname "$recorded")"
  ls -1 "$d"/*.md 2>/dev/null | LC_ALL=C sort | tail -3 | while read -r f; do
    printf '### %s\n\n```\n%s\n```\n\n' "$(basename "$f")" \
      "$(awk '/^## NOT RECORDED/{s=1;next} s&&/^```/{i=!i;next} i' "$f" \
         | head -40)"
  done
}

cmd_check() {
  local pause="$1"
  [ -e "$pause" ] || { say "not paused"; return 0; }
  local bind number stamp
  bind="$(sed -n 's/^issue: //p' "$pause" | head -1)"
  stamp="$(sed -n 's/^stamp: //p' "$pause" | head -1)"
  if [ -z "$bind" ] || [ -z "$stamp" ]; then
    say "PAUSE carries no issue binding; staying paused and filing one"
    cmd_open "$pause" "unknown" "?" \
             "$(sed -n 's/^see //p' "$pause" | head -1)" || true
    return 1
  fi
  if [ "${bind%%#*}" != "$REPO" ]; then
    say "PAUSE names ${bind%%#*} but QF_PAUSE_ISSUE_REPO is $REPO: staying paused"
    return 1
  fi
  number="${bind##*#}"

  local state
  state="$(gh_ api "repos/$REPO/issues/$number" --jq .state 2>/dev/null)" || {
    say "cannot read $bind; staying paused"; return 1; }
  [ "$state" = closed ] || { say "$bind is $state; staying paused"; return 1; }

  # THE ACTOR COMES FROM THE EVENTS API. `gh issue view --json` exposes
  # `closedAt` and `stateReason` but no `closedBy`, so it cannot answer this.
  # LATEST `closed` EVENT WINS: an issue can be closed, reopened and closed
  # again, and only the last one authorised anything.
  local actor
  actor="$(gh_ api "repos/$REPO/issues/$number/events" --paginate --jq \
             '[.[] | select(.event == "closed")] | last | .actor.login' \
           2>/dev/null)" || {
    say "cannot read the events of $bind; staying paused"; return 1; }
  case "$actor" in ""|null)
    say "$bind is closed with no closed event; staying paused"; return 1 ;;
  esac
  if ! allowed "$actor"; then
    say "$bind was closed by '$actor', who is not on the allowlist;"
    say "  staying paused"
    return 1
  fi

  # ORDERED, NOT ATOMIC (design §4.3). Four filesystem objects cannot change
  # together, and the invariant is NEVER UNPAUSED WITH STALE COUNTERS -- so the
  # directive is written, the counters are zeroed, the writes are read back, and
  # PAUSE is removed LAST. A crash before the last step leaves the loop paused
  # and the next tick retries; the reverse order would unpause a loop whose
  # streak is still at its threshold, which pauses again on the first rejection.
  cmd_directive "$number" >"$STATE/human-directive.md" || {
    say "could not persist the directive; staying paused"; return 1; }
  printf '0\n' >"$STATE/consecutive-disagreements" || return 1
  printf '0\n' >"$STATE/consecutive-verifier-failures" || return 1
  printf 'none\n' >"$STATE/last-reject-target" || return 1
  local a b c
  a="$(cat "$STATE/consecutive-disagreements" 2>/dev/null)"
  b="$(cat "$STATE/consecutive-verifier-failures" 2>/dev/null)"
  c="$(cat "$STATE/last-reject-target" 2>/dev/null)"
  if [ "$a" != 0 ] || [ "$b" != 0 ] || [ "$c" != none ]; then
    say "counters did not persist ($a/$b/$c); staying paused"
    return 1
  fi
  rm -f "$pause" || { say "cannot remove $pause"; return 1; }
  say "resumed: $bind closed by $actor (stamp $stamp)"
  return 0
}

cmd_directive() {  # cmd_directive <issue number>
  local number="$1"
  # ONLY ALLOWLISTED AUTHORS ARE PROMOTED. The research token can author
  # comments too, so an unfiltered fetch would let the loop write its own
  # instructions and hand them to itself as a human directive.
  local json
  json="$(gh_ api "repos/$REPO/issues/$number/comments" --paginate \
            --jq '.[] | [.user.login, .created_at, .body] | @tsv' \
          2>/dev/null)" || return 1
  local promoted=0 skipped=0 out=""
  while IFS=$'\t' read -r login created body; do
    [ -n "$login" ] || continue
    if allowed "$login"; then
      out+="### $login at $created

$(printf '%b' "$body")

"
      promoted=$((promoted + 1))
    else
      skipped=$((skipped + 1))
    fi
  done <<<"$json"
  [ "$promoted" = 0 ] && [ "$skipped" = 0 ] && return 0
  printf '%s\n' "## A human released the pause (INSTRUCTION, NOT EVIDENCE)" ""
  printf '%s\n' "$out"
  printf '%s\n' \
    "No figure in the block above may be cited. To use one, obtain it again" \
    "from the frontier JSON, from the tick facts, or from a command you paste" \
    "into \`Evidence:\`. The copilot has NOT been shown this block."
  [ "$skipped" = 0 ] || printf '%s\n' \
    "" "($skipped comment(s) from non-allowlisted authors were not promoted.)"
}

case "${1:-}" in
  open)  shift; cmd_open "$@" ;;
  check) shift; cmd_check "$@" ;;
  *) say "usage: pause-issue.sh open <pause> <brake> <n> <escalation> | check <pause>"
     exit 2 ;;
esac
```

- [ ] **Step 5: Run the tests**

Run: `./tests/test_pause_issue.sh`
Expected: every case passes. If a `--jq` expression is wrong the stub will not
catch it — check each one against `gh api --help` before trusting a green run.

- [ ] **Step 6: Wire it into `tick.sh`**

Move `CTX` creation and its trap (`tick.sh:204-205`) to immediately **before**
the PAUSE check, and replace the check (`:160-164`):

```bash
# THE PAUSE CHECK IS NOW A QUESTION, NOT A FULL STOP. `pause-issue.sh check`
# resumes only when an allowlisted human closed the issue this PAUSE names, and
# leaves the brake in place for every other answer -- including every API error.
if [ -e "$QF_RESEARCH/PAUSE" ]; then
  if "$HERE/pause-issue.sh" check "$QF_RESEARCH/PAUSE"; then
    say "resumed by an allowlisted close; continuing this tick"
    [ -s "$STATE/human-directive.md" ] \
      && cp "$STATE/human-directive.md" "$CTX/human-directive.md"
  else
    say "PAUSE exists; stopping"
    say "  reason: $(head -c 200 "$QF_RESEARCH/PAUSE" 2>/dev/null)"
    exit 0
  fi
fi
```

and in the context assembly, beside the `prev-escalation.md` block:

```bash
  # THE HUMAN'S RELEASE COMMENTS, leader-only and labelled non-evidence, exactly
  # like the escalation feedback. Consumed once: a directive from a pause three
  # weeks ago is not an instruction about this tick.
  if [ -s "$CTX/human-directive.md" ]; then
    head -c "$MAX_FEEDBACK_BYTES" "$CTX/human-directive.md"
    echo
    rm -f "$STATE/human-directive.md"
  fi
```

- [ ] **Step 7: Run both suites**

Run: `./tests/test_tick.sh && ./tests/test_pause_issue.sh`
Expected: PASS. The `pause_now` WARNING assertion from Task 6 Step 9 now flips —
update it to assert the issue is filed.

- [ ] **Step 8: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/pause-issue.sh \
        tools/queue-forecasting/host/research-loop/pause-resume-allowlist.txt \
        tools/queue-forecasting/host/research-loop/tick.sh \
        tools/queue-forecasting/host/tests/test_pause_issue.sh \
        tools/queue-forecasting/host/tests/test_tick.sh
git commit -m "tick: file a pause issue, and resume when a human closes it"
```

---

> **Follow-up recorded during Task 7 review, not part of that task.** `tick.sh`
> has ~25 `die` sites and a `die` writes no `PAUSE`, so a broken install (an
> unreadable prompt file, a missing workspace) leaves the loop retrying hourly
> forever with nothing raising an alarm — the 2.5-day silence of 2026-09-04 by a
> different route. The right place for this is the unit, not each call site:
> `OnFailure=` on `qf-tick.service` pointing at a one-shot that files the same
> GitHub issue §4.1 files. Consider adding it here or as an 11th task.

## Task 9: Effective-environment drift (§5)

**Files:**
- Modify: `host/phase2-setup.sh` (beside `unit_matches` / `_unit_key_filter`)
- Modify: `host/research-loop/qf-tick.service:36-39`
- Test: `host/tests/test_unit_drift.sh`

- [ ] **Step 1: Write the failing tests**

```bash
# A DECLARED KEY WHOSE VALUE DRIFTED is reported with both values.
cat >"$TMP/unit" <<'U'
[Service]
Environment=QF_TICK_MAX_DISAGREE=3
U
out="$(env_matches "$TMP/unit" "QF_TICK_MAX_DISAGREE=5")" && \
  bad "a drifted value must not pass" || ok "a drifted value must not pass"
case "$out" in *"QF_TICK_MAX_DISAGREE"*3*5*) ok "both values are reported" ;;
  *) bad "both values are reported" ;; esac

# A KEY PRESENT ONLY IN THE DROP-IN is reported BY NAME, with no value: it is
# unreviewed by definition, so its value is not ours to print.
out="$(env_matches "$TMP/unit" "QF_TICK_MAX_DISAGREE=3 QF_SECRET_THING=hunter2")"
case "$out" in
  *QF_SECRET_THING*hunter2*) bad "an unexpected value must not be printed" ;;
  *QF_SECRET_THING*)         ok  "an unexpected key is named, not valued" ;;
  *)                         bad "an unexpected key is named, not valued" ;;
esac

# NOTHING OUTSIDE QF_* IS READ, COMPARED OR PRINTED.
out="$(env_matches "$TMP/unit" "QF_TICK_MAX_DISAGREE=3 PATH=/x DATABASE_URL=secret")"
case "$out" in *DATABASE_URL*|*PATH*) bad "non-QF keys leak" ;;
  *) ok "non-QF keys are ignored" ;; esac
```

- [ ] **Step 2: Run and watch it fail**

Run: `./tests/test_unit_drift.sh 2>&1 | grep -E 'FAIL|not found'`
Expected: `env_matches: command not found`.

- [ ] **Step 3: Add `env_matches`**

In `phase2-setup.sh`, beside `unit_matches`:

```bash
# UNIT FILES ARE NOT THE WHOLE STORY, which is how the live loop came to run
# QF_TICK_MAX_DISAGREE=5 against a committed 3 for weeks. `unit_matches`
# compares the FILE; a `systemctl edit` drop-in is invisible to it. This
# compares the EFFECTIVE environment.
#
# THE EXACT KEY SET, not just the declared keys: the case a drop-in is most
# likely to produce is an entirely new knob that exists only live, and a
# comparison scoped to what the repo declares cannot see one.
#
# VALUES FOR DECLARED KEYS, NAMES ONLY FOR THE REST. A declared key is a
# reviewed threshold and printing it helps; an unexpected key's value is by
# definition unreviewed and could be anything, including a credential.
env_matches() {  # env_matches <repo unit> <effective Environment= value>
  local unit="$1" effective="$2" key value drift=0
  local -A want=() have=()
  while IFS='=' read -r key value; do
    [ -n "$key" ] && want["$key"]="$value"
  done < <(sed -n 's/^Environment=\(QF_[A-Za-z0-9_]*\)=\(.*\)$/\1=\2/p' "$unit")
  for token in $effective; do
    case "$token" in QF_*) ;; *) continue ;; esac
    have["${token%%=*}"]="${token#*=}"
  done
  for key in "${!want[@]}"; do
    if [ -z "${have[$key]+x}" ]; then
      echo "MISSING $key (repo: ${want[$key]})"; drift=1
    elif [ "${have[$key]}" != "${want[$key]}" ]; then
      echo "DRIFT $key repo=${want[$key]} live=${have[$key]}"; drift=1
    fi
  done
  for key in "${!have[@]}"; do
    [ -z "${want[$key]+x}" ] || continue
    # NAME ONLY.
    echo "UNEXPECTED $key (present live, absent from the unit)"; drift=1
  done
  return "$drift"
}
```

Call it from the same place `unit_matches` is called, with
`$(systemctl show -p Environment "$unit" | sed 's/^Environment=//')`.

- [ ] **Step 4: Add the new knobs to the committed unit**

`qf-tick.service:36-39`:

```ini
Environment=QF_REQUIRE_PREREG=1
Environment=QF_TICK_MAX_RUNS=4
Environment=QF_TICK_MAX_TICKS=12
# BOTH BRAKES, and neither is raised. 2026-09-04 auto-paused correctly on the
# first and mis-attributed nothing to the second because the second did not
# exist. A live drop-in raising this to 5 was found by `env_matches` and is
# NOT blessed here: every rejection it fired on was locally correct.
Environment=QF_TICK_MAX_DISAGREE=3
Environment=QF_TICK_MAX_VERIFIER_FAILS=3
# Two rejection episodes retire a run (design §1.3). Never 0.
Environment=QF_FRONTIER_RETIRE_AFTER=2
# PINNED HERE, in root-owned configuration, and never derived from the
# research-writable checkout's `git remote`.
Environment=QF_PAUSE_ISSUE_REPO=lotas/qf-research
# THE TOKEN PATH IS UNIT CONFIGURATION TOO. `pause-issue.sh` defaults it to
# `$HOME/.config/qf/pause-issue-token`, but the default is invisible: a deploy
# that puts it elsewhere fails at the one moment the alarm is needed, and the
# failure looks like `PAUSE exists; stopping`.
Environment=QF_PAUSE_TOKEN_FILE=/home/research/.config/qf/pause-issue-token
```

- [ ] **Step 4b: The token itself — it is in no other task**

Found by the Task 8 quality review: `QF_PAUSE_TOKEN_FILE` and the token's
creation and scopes were in no task at all. The unit line above covers the path;
this step covers the credential.

Document in `README.md` (Task 10 owns the prose, this step owns the facts): a
fine-grained PAT on `lotas/qf-research` with **Issues: read and write and
nothing else** — specifically NOT Contents, so the token cannot touch the
journal, which keeps its own `.git-credentials`. Four distinct permissions are
exercised (label create, issue create, issue read, events read) and **only the
first two fail loudly**, so a too-narrow token shows up as a pause that cannot
be released rather than as an error. Stored mode 0600, owned by `research`.

Add the preflight to `install.sh` (Task 10 Step 2 already sketches it): warn if
the token file is missing or not mode 0600, because a pause that files no issue
is the 2026-09-04 failure again — correct, and invisible.

- [ ] **Step 5: Run**

Run: `./tests/test_unit_drift.sh`
Expected: PASS.

- [ ] **Step 6: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/phase2-setup.sh \
        tools/queue-forecasting/host/research-loop/qf-tick.service \
        tools/queue-forecasting/host/tests/test_unit_drift.sh
git commit -m "setup: compare the effective QF_* environment, not just the unit file"
```

---

## Task 10: Operator documentation and deployment

**Files:**
- Modify: `host/research-loop/README.md`
- Modify: `host/research-loop/install.sh` (token and allowlist preflight)

- [ ] **Step 0: Correct the journal contract in the README**

`README.md`'s "The journal is an input too" section (~lines 111-119) documents
the *old* contract — "Only git-tracked files count... if tracked-ness cannot be
determined... nothing counts". Task 1 made the guarantee **stronger** than that:
content is read from HEAD, so a staged-but-uncommitted file no longer counts
either, and neither does a working-tree edit to a committed entry. Restate it as
committed content, and say why: the leader shares the uid that owns the journal.
Found by the Task 1 quality review, which correctly ruled it out of that task's
scope.

- [ ] **Step 1: Document the four operator actions**

Add to `README.md`:

```markdown
## When the loop pauses

An auto-PAUSE files an issue on `lotas/qf-research` titled
`PAUSED <stamp>: <brake> at <n>`. The brake name tells you which failure it was:

- `consecutive-disagreements` — the leader was rejected on N different targets.
  Research drift; read the escalations.
- `consecutive-verifier-failures` — `codex` could not produce a usable verdict N
  times running. Infrastructure; check the proxy allowlist and `codex login`.

**To release it:** comment on the issue if you want to steer the next tick, then
close it. The next tick reads the closing actor from the issue-events API and
resumes only if that login is in `pause-resume-allowlist.txt`. Your comments are
handed to the leader as an instruction, explicitly not as evidence, and are never
shown to the verifier.

Closing is the only release gesture. A comment alone changes nothing.

## When a run is retired

A run written up and rejected twice is `retired`: the report lists it under
`RETIRED:` and the leader may no longer pick it. This is deliberate —
three ticks were spent on one run on 2026-09-04 — and it means a correct finding
can be given up on.

To bring one back, commit a waiver:

```bash
cd ~/qf-research
mkdir -p journal/waivers
cat > journal/waivers/$(date -u +%Y%m%dT%H%M%SZ).md <<EOF
**Waiver:** evaluate-20260904T090941Z-0a217f40da8f-6446

Reopening: the tail result is real and the rejections were about prose.
EOF
git add journal/waivers && git commit -m "waiver: reopen the 09-04 tail result" && git push
```

The waiver starts a new episode: rejections committed before it are spent, and
two fresh ones retire the run again. Do **not** delete escalation files to
achieve this — they are the record of what the gate refused.

## Unclassified escalations

`UNCLASSIFIED ESCALATIONS: <n>` means escalations predating the
`Escalation kind:` line. They are not counted toward retirement, deliberately:
some of them are verifier outages, and retiring a run nobody judged is the harm
retirement is meant to avoid. Classify them by hand in
`journal/escalation-kinds.md`, one `<stamp> rejection|verifier-failure` per
line, and commit it.
```

- [ ] **Step 2: Preflight the token and the allowlist in `install.sh`**

```bash
# THE PAUSE PATH FAILS SILENTLY WITHOUT THESE, and a pause that files no issue
# is the 2026-09-04 failure again: correct, and invisible for two and a half
# days.
for f in "$HOME/.config/qf/pause-issue-token" \
         "$HERE/pause-resume-allowlist.txt"; do
  [ -r "$f" ] || echo "WARNING $f is missing: an auto-PAUSE will not be" \
                     "announced and cannot be released from GitHub"
done
[ ! -e "$HOME/.config/qf/pause-issue-token" ] \
  || [ "$(stat -c %a "$HOME/.config/qf/pause-issue-token")" = 600 ] \
  || echo "WARNING the pause token is not mode 0600"
```

- [ ] **Step 3: Create the token (manual, by the repository owner)**

A fine-grained PAT on `lotas/qf-research` with **Issues: read and write** and
nothing else — *not* Contents; the journal push keeps `.git-credentials`. Then:

```bash
sudo -H -u research bash -lc '
  mkdir -p ~/.config/qf && umask 077 &&
  cat > ~/.config/qf/pause-issue-token   # paste, then Ctrl-D
  chmod 600 ~/.config/qf/pause-issue-token'
```

- [ ] **Step 4: Verify end to end on the host**

```bash
# The unit is sandboxed and a hand-run tick is NOT: a hand run cannot prove a
# sandbox fix. Use systemctl.
sudo systemctl start qf-tick.service
journalctl -u qf-tick.service -n 60 --no-pager
```

Expected: `resumed by an allowlisted close` or `PAUSE exists; stopping` with a
reason, and no `EROFS` from the agent's scratch directory.

- [ ] **Step 5: Release the current pause**

The box is paused from 2026-09-04 and predates the issue mechanism, so it has no
issue to close. Release it once, by hand:

```bash
sudo -H -u research bash -lc '
  rm -f ~/qf-research/PAUSE
  printf 0 > ~/.local/state/qf-tick/consecutive-disagreements
  printf 0 > ~/.local/state/qf-tick/consecutive-verifier-failures
  printf none > ~/.local/state/qf-tick/last-reject-target'
```

- [ ] **Step 6: Commit (on request only)**

```bash
git add tools/queue-forecasting/host/research-loop/README.md \
        tools/queue-forecasting/host/research-loop/install.sh
git commit -m "docs: how to release a pause and waive a retirement"
```

---

## Self-review against the spec

| Spec section | Task |
|---|---|
| §1.1 committed content, blob oids | 1 |
| §1.2 `**Target run:**`, evaluation ids only, exactly one row | 2 (parse), 5 (prompt), 6 (`handling_of`) |
| §1.3 retirement, kinds, `unknown`, threshold ≥ 1, return shape | 2, 3 |
| §1.4 JSON / table / body / prompt, new row fields | 3, 4 |
| §1.5 waiver windowing, own directory | 2, 10 |
| §2.1 suppressible predicate, storage rules | 6 |
| §2.2 usable verdict, reset only on one | 6 |
| §2.3 fail closed, separate target helpers | 6 |
| §3.1 retry decided at startup | 7 |
| §3.2 reduced template, four bans, no cohort count | 7 |
| §4.1 open, label preflight, idempotency across states | 8 |
| §4.2 check table, events API, repo binding | 8 |
| §4.3 resume ordering, filtered directive | 8 |
| §4.4 token scope and storage | 8, 10 |
| §4.5 enforcement deferred — no task, deliberately | — |
| §5 effective `QF_*` comparison | 9 |

**Naming consistency:** `_committed_blobs`, `target_of`, `escalation_targets`,
`retire_after`, `_HANDLING_CELL` (Python); `target`, `set_target`,
`handling_of`, `suppressible`, `retry_target`, `pause_now` (bash);
`consecutive-disagreements`, `consecutive-verifier-failures`,
`last-reject-target`, `human-directive.md` (state files). Used identically in
every task above.

**Known open question for the implementer:** Task 3 Step 1's `note=` fixture
spells a multi-key pre-registration by hand. Check the separator against
`prereg.py:_KEYS` and the existing multi-key fixtures in `test_frontier.py`
before running, and follow the file rather than this plan if they differ.
