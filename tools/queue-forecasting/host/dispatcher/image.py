"""The trusted image: content-keyed, built from a context of exactly three files.

The whole point is NC12. "No qf-research file participated in the build" has to
be a fact about the build context, not a claim about our intentions -- so
`build_context` copies an explicit list out of the trusted checkout and then
asserts the resulting directory holds nothing else. A poisoned
`trainer/pyproject.toml` in the research worktree is not excluded by policy; it
is excluded because it was never copied and the assertion would fail if it had
been.

The key is content-addressed so a manifest edit produces a different image
rather than silently reusing a stale one, and it includes the pinned base digest
so a base swap cannot go unrecorded (design D11).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil

# Names AS THEY APPEAR IN THE BUILD CONTEXT, mapped from where they live in the
# trusted checkout. The Dockerfile is renamed on the way in so `docker build`
# needs no -f pointing outside the context.
CONTEXT_FILES = ("Dockerfile", "pyproject.toml", "uv.lock")
SOURCES = {
    "Dockerfile": "trainer-env.Dockerfile",
    "pyproject.toml": os.path.join("env", "pyproject.toml"),
    "uv.lock": os.path.join("env", "uv.lock"),
}

TAG_PREFIX = "qf-trainer-env"
KEY_HEX = 16
DEFAULT_TRUSTED_ROOT = "/srv/queue-forecasting"

_FROM_RE = re.compile(rb"^\s*FROM\s+(\S+)", re.MULTILINE | re.IGNORECASE)
_DIGEST_RE = re.compile(r"@sha256:([0-9a-f]{64})\Z")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")


class ImageError(Exception):
    """A build input the dispatcher will not accept."""


class UnpinnedBase(ImageError):
    """The Dockerfile's FROM carries no @sha256: digest.

    A tag is mutable, so an unpinned base means the recorded provenance
    describes whatever the registry served that minute.
    """


def trusted_root():
    return os.environ.get("QFD_TRUSTED_ROOT", DEFAULT_TRUSTED_ROOT)


def base_digest(dockerfile_bytes):
    """The pinned base digest, or raise. Read from the FIRST FROM line: this
    image is single-stage on purpose, and a multi-stage file would make
    "the base" ambiguous."""
    m = _FROM_RE.search(dockerfile_bytes)
    if not m:
        raise ImageError("the trusted Dockerfile has no FROM line")
    ref = m.group(1).decode()
    d = _DIGEST_RE.search(ref)
    if not d:
        raise UnpinnedBase(
            f"base image {ref!r} is not pinned by digest; run"
            " `phase2-setup.sh pin-base` and paste the line it prints")
    return d.group(1)


def _read(trusted_dir, name):
    path = os.path.join(trusted_dir, SOURCES[name])
    if not os.path.isfile(path):
        raise ImageError(f"missing trusted build input: {path}")
    with open(path, "rb") as fh:
        return fh.read()


def content_key(trusted_dir):
    """sha256 over the pinned base digest and the three files, each
    LENGTH-PREFIXED so concatenation is unambiguous.

    Without the length prefix, moving a byte from the end of pyproject.toml to
    the start of uv.lock leaves the concatenation identical and the key
    unchanged -- two different environments sharing one image.
    """
    blobs = {name: _read(trusted_dir, name) for name in CONTEXT_FILES}
    h = hashlib.sha256()
    digest = base_digest(blobs["Dockerfile"]).encode()
    h.update(b"base:%d:" % len(digest) + digest)
    for name in CONTEXT_FILES:          # fixed order, not directory order
        data = blobs[name]
        h.update(b"%s:%d:" % (name.encode(), len(data)) + data)
    return h.hexdigest()[:KEY_HEX]


def tag_for(trusted_dir):
    return f"{TAG_PREFIX}:{content_key(trusted_dir)}"


def build_context(trusted_dir, tmpdir, trusted_root_dir=None):
    """Copy exactly CONTEXT_FILES into `tmpdir` and assert nothing else is there.

    The assertion is NC12's mechanism. It is not a sanity check: it is what makes
    "no qf-research file participated in the build" a fact rather than a claim.
    """
    root = os.path.realpath(trusted_root_dir or trusted_root())
    real = os.path.realpath(trusted_dir)
    # NC10: a trusted path must resolve INSIDE the trusted checkout. A symlink
    # in a run directory pointing at the research worktree is the attack.
    if real != root and not real.startswith(root + os.sep):
        raise ImageError(
            f"trusted_dir {trusted_dir!r} resolves to {real!r}, outside the"
            f" trusted root {root!r}")

    os.makedirs(tmpdir, exist_ok=True)
    for name in CONTEXT_FILES:
        src = os.path.join(real, SOURCES[name])
        if not os.path.isfile(src):
            raise ImageError(f"missing trusted build input: {src}")
        if os.path.islink(src):
            raise ImageError(f"trusted build input is a symlink: {src}")
        shutil.copyfile(src, os.path.join(tmpdir, name))

    listing = tuple(sorted(os.listdir(tmpdir)))
    if listing != tuple(sorted(CONTEXT_FILES)):
        raise ImageError(
            f"build context holds {listing}, expected exactly"
            f" {tuple(sorted(CONTEXT_FILES))}")
    return tmpdir


def ensure_image(trusted_dir, runner, tmpdir=None, trusted_root_dir=None):
    """Return (tag, image_digest), building only if the tag is missing.

    `image_digest` is the image config **Id**, not a RepoDigest: a locally built
    image has no RepoDigests at all, so recording one would record an empty
    string (design D11). The Id is also what `sandbox.py` demands as its
    image_ref, so a tag cannot be re-pointed between here and the create.
    """
    tag = tag_for(trusted_dir)
    inspect = ["docker", "image", "inspect", "--format", "{{.Id}}", tag]
    got = runner(inspect, None)
    if got.returncode != 0:
        if tmpdir is None:
            raise ImageError("ensure_image needs a tmpdir to build into")
        ctx = build_context(trusted_dir, tmpdir, trusted_root_dir)
        # Classic builder (design D10): BuildKit's result can stay in a build
        # cache rather than the image store, which is exactly what the
        # inspect-after-build assertion below would catch.
        built = runner(["docker", "build", "-t", tag, ctx],
                       {"DOCKER_BUILDKIT": "0"})
        if built.returncode != 0:
            raise ImageError(
                f"docker build failed: {(built.stderr or '').strip()[-2000:]}")
        got = runner(inspect, None)
        if got.returncode != 0:
            raise ImageError(
                "the image is not inspectable after a successful build;"
                " the builder left it somewhere other than the image store")

    image_id = (got.stdout or "").strip()
    if not _IMAGE_ID_RE.match(image_id):
        raise ImageError(f"docker returned an unusable image id: {image_id!r}")
    return tag, image_id
