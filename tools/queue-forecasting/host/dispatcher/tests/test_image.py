# Tests for image.py. The key computation is pure; the docker calls go through
# an injected runner, so this suite needs no daemon and no privileges.
import os
import subprocess
import tempfile
import types
import unittest

import image

DIGEST = "a" * 64
DOCKERFILE = (f"FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:{DIGEST}\n"
              "RUN uv sync --locked --no-install-project\n").encode()


def proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                 stderr=stderr)


class ImageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.trusted = os.path.join(self.root, "dispatcher")
        os.makedirs(os.path.join(self.trusted, "env"))
        self.write("trainer-env.Dockerfile", DOCKERFILE)
        self.write("env/pyproject.toml", b"[project]\nname='x'\n")
        self.write("env/uv.lock", b"version = 1\n")

    def write(self, rel, data):
        path = os.path.join(self.trusted, rel)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def key(self):
        return image.content_key(self.trusted)


class TestContentKey(ImageCase):
    def test_a_one_byte_change_to_any_file_changes_the_key(self):
        # A stale image silently reused after a manifest edit is the failure.
        before = self.key()
        for rel, data in [("env/pyproject.toml", b"[project]\nname='y'\n"),
                          ("env/uv.lock", b"version = 2\n"),
                          ("trainer-env.Dockerfile", DOCKERFILE + b"# x\n")]:
            with self.subTest(file=rel):
                with open(os.path.join(self.trusted, rel), "rb") as fh:
                    original = fh.read()
                self.write(rel, data)
                self.assertNotEqual(before, self.key(), f"{rel} did not move it")
                self.write(rel, original)
        self.assertEqual(before, self.key())

    def test_changing_the_pinned_base_digest_changes_the_key(self):
        # A base-image swap must not go unrecorded.
        before = self.key()
        self.write("trainer-env.Dockerfile",
                   DOCKERFILE.replace(DIGEST.encode(), b"b" * 64))
        self.assertNotEqual(before, self.key())

    def test_the_key_is_stable_across_calls_and_ignores_mtime(self):
        # Spurious rebuilds cost 20 minutes each.
        before = self.key()
        os.utime(os.path.join(self.trusted, "env", "uv.lock"),
                 (1000000000, 1000000000))
        self.assertEqual(before, self.key())
        self.assertEqual(self.key(), self.key())

    def test_length_prefixing_defeats_a_concatenation_collision(self):
        # Moving a byte from the end of pyproject.toml to the start of uv.lock
        # leaves the naive concatenation identical.
        self.write("env/pyproject.toml", b"AAAAX")
        self.write("env/uv.lock", b"BBBB")
        a = self.key()
        self.write("env/pyproject.toml", b"AAAA")
        self.write("env/uv.lock", b"XBBBB")
        self.assertNotEqual(a, self.key())

    def test_the_key_is_sixteen_hex_characters(self):
        k = self.key()
        self.assertEqual(len(k), 16)
        self.assertRegex(k, r"^[0-9a-f]{16}$")

    def test_the_tag_carries_the_key(self):
        self.assertEqual(image.tag_for(self.trusted),
                         f"qf-trainer-env:{self.key()}")


class TestBasePinning(ImageCase):
    def test_an_undigested_from_is_rejected(self):
        self.write("trainer-env.Dockerfile",
                   b"FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim\n")
        with self.assertRaises(image.UnpinnedBase):
            self.key()

    def test_a_tag_that_merely_mentions_sha256_is_rejected(self):
        self.write("trainer-env.Dockerfile", b"FROM foo:sha256-abc\n")
        with self.assertRaises(image.UnpinnedBase):
            self.key()

    def test_a_short_digest_is_rejected(self):
        self.write("trainer-env.Dockerfile", b"FROM foo@sha256:abcd\n")
        with self.assertRaises(image.UnpinnedBase):
            self.key()

    def test_a_dockerfile_with_no_from_is_rejected(self):
        self.write("trainer-env.Dockerfile", b"RUN true\n")
        with self.assertRaises(image.ImageError):
            self.key()

    def test_the_shipped_dockerfile_is_pinned_by_digest(self):
        # This assertion INVERTED when Task 11 was performed: `pin-base` printed
        # a real digest, it was committed, and the shipped file is now pinned.
        # Keeping the old expectation would have meant a test asserting the
        # deployment had not happened.
        #
        # Still worth a test, in the new direction: it catches a revert to the
        # placeholder and a hand-edit back to a floating tag, either of which
        # would move the base out from under the content key. The refusal path
        # does not depend on what the repo ships -- the four cases above cover it
        # with synthetic input.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "trainer-env.Dockerfile"), "rb") as fh:
            shipped = fh.read()
        digest = image.base_digest(shipped)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")


class TestBuildContext(ImageCase):
    def ctx(self, **kw):
        dest = os.path.join(self.root, "ctx")
        return image.build_context(self.trusted, dest,
                                   kw.pop("trusted_root_dir", self.root), **kw)

    def test_extra_files_in_the_trusted_dir_do_not_enter_the_context(self):
        # The poisoned-manifest path (NC12): a file that is not on the list is
        # not excluded by policy, it is simply never copied.
        self.write("poison.txt", b"x")
        os.makedirs(os.path.join(self.trusted, "tests"), exist_ok=True)
        self.write("tests/conftest.py", b"import os; os.system('id')")
        dest = self.ctx()
        self.assertEqual(sorted(os.listdir(dest)),
                         sorted(image.CONTEXT_FILES))

    def test_the_context_contents_match_the_trusted_sources(self):
        dest = self.ctx()
        with open(os.path.join(dest, "Dockerfile"), "rb") as fh:
            self.assertEqual(fh.read(), DOCKERFILE)
        with open(os.path.join(dest, "uv.lock"), "rb") as fh:
            self.assertEqual(fh.read(), b"version = 1\n")

    def test_a_missing_required_file_raises(self):
        os.remove(os.path.join(self.trusted, "env", "uv.lock"))
        with self.assertRaises(image.ImageError):
            self.ctx()

    def test_a_symlinked_build_input_is_refused(self):
        target = os.path.join(self.root, "outside.lock")
        with open(target, "wb") as fh:
            fh.write(b"evil")
        link = os.path.join(self.trusted, "env", "uv.lock")
        os.remove(link)
        os.symlink(target, link)
        with self.assertRaises(image.ImageError):
            self.ctx()

    def test_a_trusted_dir_outside_the_trusted_root_is_refused(self):
        # NC10. The realpath is what is checked, so a symlink does not help.
        outside = os.path.join(self.root, "..", "elsewhere")
        with self.assertRaises(image.ImageError):
            image.build_context(outside, os.path.join(self.root, "ctx2"),
                                os.path.join(self.root, "nested"))

    def test_a_symlinked_trusted_dir_escaping_the_root_is_refused(self):
        real_root = os.path.join(self.root, "root")
        os.makedirs(real_root)
        link = os.path.join(real_root, "sneaky")
        os.symlink(self.trusted, link)
        with self.assertRaises(image.ImageError):
            image.build_context(link, os.path.join(self.root, "ctx3"),
                                real_root)


class TestEnsureImage(ImageCase):
    def setUp(self):
        super().setUp()
        self.calls = []

    def runner_factory(self, inspect_results):
        results = list(inspect_results)

        def runner(argv, env):
            self.calls.append((argv, env))
            if argv[:3] == ["docker", "image", "inspect"]:
                return results.pop(0)
            if argv[:2] == ["docker", "build"]:
                return proc(0, "built")
            raise AssertionError(f"unexpected command {argv}")
        return runner

    def test_a_present_image_is_not_rebuilt(self):
        runner = self.runner_factory([proc(0, f"sha256:{'c' * 64}\n")])
        tag, digest = image.ensure_image(self.trusted, runner)
        self.assertEqual(tag, image.tag_for(self.trusted))
        self.assertEqual(digest, f"sha256:{'c' * 64}")
        self.assertFalse([c for c in self.calls if c[0][:2] == ["docker", "build"]])

    def test_a_missing_image_is_built_then_inspected(self):
        # Trivially true for the classic builder, kept because it is exactly
        # what breaks if anyone switches to a driver whose result stays in a
        # build cache (design D10).
        runner = self.runner_factory([proc(1, "", "No such image"),
                                      proc(0, f"sha256:{'d' * 64}\n")])
        tag, digest = image.ensure_image(
            self.trusted, runner, tmpdir=os.path.join(self.root, "ctx"),
            trusted_root_dir=self.root)
        self.assertEqual(digest, f"sha256:{'d' * 64}")
        builds = [c for c in self.calls if c[0][:2] == ["docker", "build"]]
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][1], {"DOCKER_BUILDKIT": "0"})
        self.assertEqual(builds[0][0][:4], ["docker", "build", "-t", tag])

    def test_an_image_that_is_not_inspectable_after_a_build_raises(self):
        runner = self.runner_factory([proc(1, "", "No such image"),
                                      proc(1, "", "No such image")])
        with self.assertRaises(image.ImageError):
            image.ensure_image(self.trusted, runner,
                               tmpdir=os.path.join(self.root, "ctx"),
                               trusted_root_dir=self.root)

    def test_a_failed_build_raises(self):
        def runner(argv, env):
            if argv[:3] == ["docker", "image", "inspect"]:
                return proc(1, "", "No such image")
            return proc(2, "", "uv sync failed: lock is out of date")
        with self.assertRaises(image.ImageError) as cm:
            image.ensure_image(self.trusted, runner,
                               tmpdir=os.path.join(self.root, "ctx"),
                               trusted_root_dir=self.root)
        self.assertIn("lock is out of date", str(cm.exception))

    def test_a_non_id_inspect_result_is_refused(self):
        # A tag would let the image be re-pointed between here and docker run.
        runner = self.runner_factory([proc(0, "qf-trainer-env:latest\n")])
        with self.assertRaises(image.ImageError):
            image.ensure_image(self.trusted, runner)


class TestPromotedManifestsMatchTheTrainer(unittest.TestCase):
    def test_the_promoted_copies_are_byte_identical_to_the_trainers(self):
        # Refreshing them is a reviewed act with a diff, never a sync. This test
        # is the tripwire that says a review is due -- it does NOT copy.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        qf = os.path.dirname(os.path.dirname(here))     # tools/queue-forecasting
        for name in ("pyproject.toml", "uv.lock"):
            with self.subTest(manifest=name):
                with open(os.path.join(here, "env", name), "rb") as fh:
                    promoted = fh.read()
                with open(os.path.join(qf, "trainer", name), "rb") as fh:
                    current = fh.read()
                self.assertEqual(
                    promoted, current,
                    f"host/dispatcher/env/{name} differs from trainer/{name}."
                    " Review the diff and promote deliberately; do not sync.")


if __name__ == "__main__":
    unittest.main()
