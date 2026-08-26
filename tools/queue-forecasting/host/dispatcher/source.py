"""The trusted mirror of the research repository, and worktrees cut from it.

Three rules, each of which is a negative control somewhere:

  1. The mirror is OURS, not the agent's. The objects landing in it are authored
     by the agent, so `core.hooksPath=/dev/null` is set at creation: a repo-side
     hook must never run as `qfd` (design D3).
  2. A SHA is only usable if it is REACHABLE from a remote-tracking ref.
     `git cat-file -e` accepts a force-dropped commit no human can look at; a
     verdict about such a commit has no URL behind it.
  3. The token never appears in argv, in a URL, or in a log. It is read from a
     mode-0400 file by a credential helper at the moment git asks for it
     (auto-research-phase1-design.md §7.2, carried forward).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
REMOTE_NAMESPACE = "refs/remotes/origin"


class SourceError(Exception):
    """A git operation the dispatcher will not paper over."""


class NotPublished(SourceError):
    """The SHA is not reachable from any remote-tracking ref.

    Either it was never pushed, or it was force-dropped. Both mean the same
    thing to a reviewer: there is no URL at which a human can read this commit,
    so nothing may be concluded from running it.
    """


DEFAULT_TIMEOUT_S = 300


class Timeout(SourceError):
    """A git command exceeded its bound.

    This is its own class because the caller has to distinguish it: a fetch that
    HANGS while the training mutex is held is the one failure mode that can blow
    through every deadline downstream of it, so the runner turns this into a
    deadline failure rather than a retry.
    """


class Source:
    def __init__(self, mirror_dir, remote_url, token_path=None, runner=None,
                 timeout_s=DEFAULT_TIMEOUT_S):
        self.mirror_dir = str(mirror_dir)
        self.remote_url = remote_url
        self.token_path = str(token_path) if token_path else None
        # A PER-COMMAND ceiling. It is not a total deadline, and the difference
        # matters: `resolve` runs several commands, so five of them each honouring
        # a 300s ceiling can spend 1500s while the job holds the training mutex.
        # Callers that have a real budget pass `deadline` (an ABSOLUTE instant)
        # to the public methods below, and every command then gets
        # min(ceiling, time to the deadline).
        #
        # The deadline is a PARAMETER, never instance state. An earlier version
        # had the runner assign `src.timeout_s` before each call, which is shared
        # mutable state on an object both light workers use -- so two jobs
        # overwrote each other's budget, and whichever wrote last decided how
        # long the other could run.
        self.timeout_s = timeout_s
        # Every command this object runs, for the test that asserts the token
        # is not in any of them. It is a list of argv lists, never a string.
        self.command_log = []
        self._runner = runner or self._subprocess_runner

    # --- plumbing --------------------------------------------------------
    @staticmethod
    def _subprocess_runner(argv, cwd, env, timeout=None):
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=timeout)

    def _env(self):
        env = dict(os.environ)
        # Fail rather than block forever on a credential prompt in a unit with
        # no terminal.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        return env

    def _git(self, *args, cwd=None, check=True, timeout=None, deadline=None):
        argv = ["git", *args]
        self.command_log.append(list(argv))
        budget = self.timeout_s if timeout is None else timeout
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise Timeout(
                    f"git {' '.join(args[:3])} not started: the deadline had"
                    " already passed")
            budget = min(budget, remaining)
        try:
            proc = self._runner(argv, cwd or self.mirror_dir, self._env(),
                                budget)
        except subprocess.TimeoutExpired:
            raise Timeout(
                f"git {' '.join(args[:3])} exceeded {budget:.0f}s; the remote is"
                " unreachable or tarpitting, and this job holds the training"
                " mutex")
        if check and proc.returncode != 0:
            raise SourceError(
                f"git {' '.join(args[:3])} failed ({proc.returncode}):"
                f" {(proc.stderr or '').strip()}")
        return proc

    def _credential_helper(self):
        """A helper git invokes when it needs the password. The FILE PATH is in
        argv; the token is read from the file inside the helper and therefore
        never is."""
        quoted = shlex.quote(self.token_path)
        return ("!f() { test \"$1\" = get || exit 0;"
                " echo username=x-access-token;"
                f" echo \"password=$(cat {quoted})\"; }}; f")

    # --- mirror ----------------------------------------------------------
    def ensure_mirror(self):
        """Idempotent. Safe to call on every fetch."""
        if not os.path.isdir(os.path.join(self.mirror_dir, "objects")):
            os.makedirs(self.mirror_dir, exist_ok=True)
            self._git("init", "--bare", "--quiet", self.mirror_dir, cwd=".")
        # Set unconditionally, not only at creation: a mirror that predates this
        # rule, or one edited by hand, must be corrected rather than trusted.
        self._git("config", "core.hooksPath", "/dev/null")
        self._git("config", "gc.auto", "0")
        existing = self._git("remote", check=False).stdout or ""
        if "origin" in existing.split():
            self._git("remote", "set-url", "origin", self.remote_url)
        else:
            self._git("remote", "add", "origin", self.remote_url)
        return self.mirror_dir

    def fetch(self, deadline=None):
        args = []
        if self.token_path:
            # The empty value first clears any inherited helper, so a
            # system-level helper cannot answer instead of ours.
            args += ["-c", "credential.helper=",
                     "-c", "credential.helper=" + self._credential_helper()]
        args += ["fetch", "--prune", "--quiet", "origin", FETCH_REFSPEC]
        self._git(*args, deadline=deadline)

    # --- reachability ----------------------------------------------------
    def _has_object(self, sha, deadline=None):
        return self._git("cat-file", "-e", f"{sha}^{{commit}}",
                         check=False, deadline=deadline).returncode == 0

    def _containing_refs(self, sha, deadline=None):
        out = self._git("for-each-ref", "--format=%(refname)",
                        f"--contains={sha}", REMOTE_NAMESPACE,
                        check=False, deadline=deadline)
        if out.returncode != 0:
            return []
        return [line for line in (out.stdout or "").splitlines() if line.strip()]

    def resolve(self, sha, deadline=None):
        """Return the remote-tracking ref `sha` is reachable from, or raise.

        **Fetches first, exactly once, unconditionally.** Reachability is a
        question about the remote AS IT IS NOW, and it cannot be answered from
        refs the mirror happens to be holding.

        Two earlier versions got this wrong in the same direction. The first
        fetched only when the OBJECT was missing. The second also fetched when
        no local ref contained the object -- which still fails the case that
        matters: after an upstream force-rewind, the mirror's stale
        `refs/remotes/origin/main` STILL CONTAINS the dropped commit, so the
        reachability check finds a ref, returns happily, and the dispatcher runs
        a commit no human can read at any URL. That is precisely what D3 exists
        to reject, and a stale success is worse than a stale failure because
        nothing ever revisits it.

        `fetch()` is `--prune`, so after it the remote-tracking refs are the
        remote's. One fetch per resolve is the cost of an honest answer; the
        bound the earlier wording cared about -- a typo must not become an
        unbounded fetch LOOP -- is still met, because this fetches once and then
        decides.
        """
        self.fetch(deadline=deadline)
        if not self._has_object(sha, deadline=deadline):
            raise NotPublished(
                f"{sha} is not in the mirror after a fetch; it was never"
                " pushed to the research remote")
        refs = self._containing_refs(sha, deadline=deadline)
        if not refs:
            raise NotPublished(
                f"{sha} exists in the object store but is not reachable from any"
                f" {REMOTE_NAMESPACE}/* ref after a pruning fetch; it was"
                " force-dropped and no human can read it at a URL")
        # Deterministic pick, so the recorded ref does not depend on git's
        # iteration order between two identical runs.
        return sorted(refs)[0]

    # --- worktrees -------------------------------------------------------
    def add_worktree(self, sha, dest, deadline=None):
        """Materialise the exact tree of `sha` at `dest`, detached.

        No submodule init: a submodule URL is agent-controlled and would be a
        network fetch of unreviewed content. Nothing is run inside `dest` after
        the checkout.
        """
        dest = str(dest)
        self._git("worktree", "add", "--detach", "--no-checkout", dest, sha,
                  deadline=deadline)
        self._git("-C", dest, "checkout", "--quiet", "--detach", sha,
                  deadline=deadline)
        return dest

    def remove_worktree(self, dest):
        """Remove and prune. Pruning matters: leftover `worktrees/` metadata
        makes a later `worktree add` at the same path fail, which would wedge
        the run directory for every subsequent job."""
        dest = str(dest)
        self._git("worktree", "remove", "--force", dest, check=False)
        self._git("worktree", "prune")
