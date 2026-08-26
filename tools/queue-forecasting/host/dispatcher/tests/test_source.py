# Tests for source.py against a real local fixture repository: `git init` in a
# temp dir, no network, no token needed to reach it. Real git is the point --
# reachability semantics are git's, and a mock would encode our belief about
# them rather than test it.
import os
import subprocess
import tempfile
import unittest

import source

SECRET = "ghp_thisMustNeverAppearInAnyCommandLine"


IDENT = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    # Fixed instants: two runs of this suite must produce the same commit ids,
    # so a failure is reproducible from the message alone.
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **IDENT})
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr}")
    return p.stdout.strip()


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.upstream = os.path.join(self.root, "upstream")
        self.mirror = os.path.join(self.root, "mirror.git")
        os.makedirs(self.upstream)
        git(self.upstream, "init", "--quiet", "-b", "main")
        git(self.upstream, "config", "user.email", "t@example.com")
        git(self.upstream, "config", "user.name", "t")
        self.token_path = os.path.join(self.root, "token")
        with open(self.token_path, "w") as fh:
            fh.write(SECRET)
        os.chmod(self.token_path, 0o400)
        self.src = source.Source(self.mirror, self.upstream, self.token_path)

    def commit(self, name, content="x", branch="main"):
        """Build the commit with plumbing rather than `git commit`.

        Same objects, and it does not depend on the index/HEAD dance that
        switching branches would need -- a branch here is just a ref update.
        """
        with open(os.path.join(self.upstream, name), "w") as fh:
            fh.write(content)
        git(self.upstream, "add", name)
        tree = git(self.upstream, "write-tree")
        parent = git(self.upstream, "rev-parse", "--verify", "--quiet",
                     f"refs/heads/{branch}", check=False)
        args = ["commit-tree", tree, "-m", f"add {name}"]
        if parent:
            args += ["-p", parent]
        sha = git(self.upstream, *args)
        git(self.upstream, "update-ref", f"refs/heads/{branch}", sha)
        return sha


class TestMirror(SourceCase):
    def test_ensure_mirror_is_idempotent_and_disables_hooks(self):
        # An agent-authored hook must never run as qfd (design D3).
        self.src.ensure_mirror()
        self.assertEqual(git(self.mirror, "config", "core.hooksPath"),
                         "/dev/null")
        self.src.ensure_mirror()          # again: must not raise
        self.assertEqual(git(self.mirror, "config", "core.hooksPath"),
                         "/dev/null")
        self.assertEqual(git(self.mirror, "remote"), "origin")

    def test_ensure_mirror_corrects_a_hooks_path_edited_by_hand(self):
        self.src.ensure_mirror()
        git(self.mirror, "config", "core.hooksPath", "/tmp/evil")
        self.src.ensure_mirror()
        self.assertEqual(git(self.mirror, "config", "core.hooksPath"),
                         "/dev/null")

    def test_the_mirror_is_bare(self):
        self.src.ensure_mirror()
        self.assertEqual(git(self.mirror, "rev-parse", "--is-bare-repository"),
                         "true")


class TestResolve(SourceCase):
    def setUp(self):
        super().setUp()
        self.sha = self.commit("a.txt")
        self.src.ensure_mirror()
        self.src.fetch()

    def test_resolve_returns_the_ref_a_reachable_commit_sits_on(self):
        # A verdict needs a URL behind it, and the ref is what supplies one.
        self.assertEqual(self.src.resolve(self.sha),
                         "refs/remotes/origin/main")

    def test_resolve_raises_for_a_commit_that_is_present_but_unreachable(self):
        # Force-dropped upstream: the object survives in our mirror, but no
        # human can look at it at any URL (design D3).
        git(self.upstream, "update-ref", "refs/heads/tmp",
            git(self.upstream, "rev-parse", "refs/heads/main"))
        dropped = self.commit("b.txt", branch="tmp")
        self.src.fetch()
        self.assertTrue(self.src._has_object(dropped))
        git(self.upstream, "update-ref", "-d", "refs/heads/tmp")
        self.src.fetch()                      # --prune drops the tracking ref
        self.assertTrue(self.src._has_object(dropped),
                        "fixture broken: the object should still be present")
        with self.assertRaises(source.NotPublished):
            self.src.resolve(dropped)

    def test_resolve_raises_for_an_unknown_sha_after_exactly_one_fetch(self):
        # A typo must not become an unbounded fetch loop.
        self.src.command_log.clear()
        with self.assertRaises(source.NotPublished):
            self.src.resolve("0" * 40)
        fetches = [c for c in self.src.command_log if "fetch" in c]
        self.assertEqual(len(fetches), 1, self.src.command_log)

    def test_resolve_fetches_a_commit_pushed_after_the_last_fetch(self):
        later = self.commit("c.txt")
        self.assertEqual(self.src.resolve(later), "refs/remotes/origin/main")


class TestWorktree(SourceCase):
    def setUp(self):
        super().setUp()
        self.first = self.commit("a.txt", "first")
        self.second = self.commit("a.txt", "second")
        self.src.ensure_mirror()
        self.src.fetch()
        self.dest = os.path.join(self.root, "wt")

    def test_add_worktree_produces_the_exact_tree_of_that_sha(self):
        self.src.add_worktree(self.first, self.dest)
        with open(os.path.join(self.dest, "a.txt")) as fh:
            self.assertEqual(fh.read(), "first")
        self.assertEqual(git(self.dest, "rev-parse", "HEAD"), self.first)

    def test_add_worktree_leaves_the_mirrors_head_alone(self):
        # Cross-contamination between concurrent runs is the failure.
        before = git(self.mirror, "symbolic-ref", "-q", "HEAD", check=False)
        self.src.add_worktree(self.first, self.dest)
        after = git(self.mirror, "symbolic-ref", "-q", "HEAD", check=False)
        self.assertEqual(before, after)

    def test_two_worktrees_at_different_shas_coexist(self):
        other = os.path.join(self.root, "wt2")
        self.src.add_worktree(self.first, self.dest)
        self.src.add_worktree(self.second, other)
        with open(os.path.join(self.dest, "a.txt")) as fh:
            self.assertEqual(fh.read(), "first")
        with open(os.path.join(other, "a.txt")) as fh:
            self.assertEqual(fh.read(), "second")

    def test_remove_then_add_at_the_same_path_succeeds(self):
        # Stale worktrees/ metadata would otherwise wedge the run directory for
        # every later job.
        self.src.add_worktree(self.first, self.dest)
        self.src.remove_worktree(self.dest)
        self.assertFalse(os.path.exists(self.dest))
        self.src.add_worktree(self.second, self.dest)
        with open(os.path.join(self.dest, "a.txt")) as fh:
            self.assertEqual(fh.read(), "second")

    def test_remove_worktree_tolerates_a_directory_already_gone(self):
        self.src.add_worktree(self.first, self.dest)
        subprocess.run(["rm", "-rf", self.dest], check=True)
        self.src.remove_worktree(self.dest)      # must not raise
        self.src.add_worktree(self.second, self.dest)


class TestTokenNeverLeaks(SourceCase):
    def test_the_token_contents_appear_in_no_command_line(self):
        # Phase 1 §7.2, carried forward. The FILE PATH may appear; the secret
        # must not.
        self.commit("a.txt")
        self.src.ensure_mirror()
        self.src.fetch()
        self.src.resolve(git(self.upstream, "rev-parse", "HEAD"))
        flat = " ".join(" ".join(c) for c in self.src.command_log)
        self.assertNotIn(SECRET, flat)
        self.assertTrue(self.src.command_log, "nothing was captured")

    def test_the_helper_references_the_path_and_not_the_secret(self):
        helper = self.src._credential_helper()
        self.assertIn(self.token_path, helper)
        self.assertNotIn(SECRET, helper)

    def test_the_remote_url_carries_no_credentials(self):
        self.src.ensure_mirror()
        self.assertNotIn("@", git(self.mirror, "remote", "get-url", "origin"))


if __name__ == "__main__":
    unittest.main()
