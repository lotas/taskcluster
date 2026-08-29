# Tests for sandbox.py. Pure argv construction, so these assert the boundary
# directly: a flag that disappears from the list is a hole in D2, and the
# negative cases are the compose habits that must not follow the trainer in.
import unittest

import sandbox
import spec

IMAGE = "sha256:" + "a" * 64
SHA = "3f1c" + "0" * 36


def argv(**over):
    kw = dict(image_ref=IMAGE, run_id="test-20260825T101112Z-3f1c00000000-7",
              spec_hash="b" * 64, kind="test", src_mount="/var/lib/qf-runs/r/src",
              out_mount="/var/lib/qf-runs/r/out",
              entrypoint_argv=["/opt/qfenv/bin/python", "-m", "pytest"],
              mem_limit="4g", cpus=4.0)
    kw.update(over)
    return sandbox.docker_create_argv(**kw)


def pairs(a):
    """Flag/value pairs, so a test can assert on ['--memory', '4096m'] without
    caring about position."""
    return list(zip(a, a[1:]))


class TestRequiredFlags(unittest.TestCase):
    def test_every_boundary_flag_is_present(self):
        a = argv()
        p = pairs(a)
        for flag, value in [("--network", "none"), ("--user", "10001:10001"),
                            ("--cap-drop", "ALL"),
                            ("--security-opt", "no-new-privileges"),
                            ("--pids-limit", "512"),
                            ("--oom-score-adj", "500"),
                            ("--log-driver", "none")]:
            with self.subTest(flag=flag):
                self.assertIn((flag, value), p)
        self.assertIn("--read-only", a)
        self.assertIn("--rm", a)

    def test_memory_swap_equals_memory(self):
        # Swap turns a memory cap into a thrash, and the OOM the accounting
        # relies on never arrives.
        p = pairs(argv(mem_limit="8g"))
        self.assertIn(("--memory", "8192m"), p)
        self.assertIn(("--memory-swap", "8192m"), p)

    def test_log_driver_none_is_always_present(self):
        # A capped log file is pointless while Docker's own store grows
        # unbounded (design §4.5).
        for kind in ("test", "selftest"):
            self.assertIn(("--log-driver", "none"), pairs(argv(kind=kind)))

    def test_tmpfs_is_nosuid_nodev_and_sized(self):
        p = pairs(argv(tmpfs_size="512m"))
        self.assertIn(("--tmpfs", "/tmp:rw,nosuid,nodev,size=512m"), p)

    def test_a_malformed_tmpfs_size_raises(self):
        for bad in ["1G", "0g", "", "1gb", "big"]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(tmpfs_size=bad)


class TestNoComposeHabits(unittest.TestCase):
    def test_no_env_file_no_database_url_no_docker_socket(self):
        # docker-compose.yml gives the trainer env_file, DATABASE_URL and a
        # read-write mount. None of it survives into the sandbox.
        flat = " ".join(argv())
        for forbidden in ["--env-file", "DATABASE_URL", "docker.sock",
                          "/var/run/docker.sock", "--privileged", "--env",
                          "--network host"]:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, flat)

    def test_no_bare_e_flag_is_emitted(self):
        self.assertNotIn("-e", argv())

    def test_the_source_mount_is_read_only(self):
        a = argv()
        self.assertIn(f"/var/lib/qf-runs/r/src:{sandbox.SRC_DEST}:ro", a)

    def test_the_out_mount_is_read_write(self):
        self.assertIn(f"/var/lib/qf-runs/r/out:{sandbox.OUT_DEST}:rw", argv())


class TestMounts(unittest.TestCase):
    def test_a_relative_mount_source_raises(self):
        # A relative path is resolved against the daemon's cwd, not ours.
        with self.assertRaises(sandbox.SandboxError):
            argv(src_mount="runs/r/src")
        with self.assertRaises(sandbox.SandboxError):
            argv(out_mount="./out")

    def test_a_mount_source_containing_a_colon_raises(self):
        with self.assertRaises(sandbox.SandboxError):
            argv(src_mount="/a/b:c")

    def test_a_destination_outside_the_allowlist_raises(self):
        # A job mounting over /opt/qfenv would replace the interpreter the
        # entrypoint names.
        for dest in ["/opt/qfenv", "/", "/etc", "/app/trainer", "/out",
                     "/trusted/../opt/qfenv", "/artifactsX"]:
            with self.subTest(dest=dest), self.assertRaises(sandbox.SandboxError):
                argv(extra_ro_mounts=[("/srv/x", dest)])

    def test_an_allowlisted_extra_is_emitted(self):
        a = argv(extra_ro_mounts=[("/srv/qf/nc13-inside.sh",
                                   "/trusted/nc13-inside.sh")])
        self.assertIn("/srv/qf/nc13-inside.sh:/trusted/nc13-inside.sh:ro", a)

    def test_the_artifacts_destination_is_allowed_read_write(self):
        a = argv(role="handoff", extra_rw_mounts=[("/var/lib/qf-runs/r/artifacts",
                                                   "/artifacts")])
        self.assertIn("/var/lib/qf-runs/r/artifacts:/artifacts:rw", a)


class TestImageRef(unittest.TestCase):
    def test_a_tag_or_bare_name_is_refused(self):
        # A tag race between the recorded digest and the started image.
        for bad in ["qf-trainer-env:abc", "qf-trainer-env", "a" * 64,
                    "sha256:short", "", None, "SHA256:" + "a" * 64]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(image_ref=bad)

    def test_the_image_ref_is_the_last_element_before_the_entrypoint(self):
        a = argv(entrypoint_argv=["/bin/true"])
        self.assertEqual(a[-2:], [IMAGE, "/bin/true"])


class TestUser(unittest.TestCase):
    def test_user_defaults_to_the_unprivileged_identity(self):
        self.assertIn(("--user", "10001:10001"), pairs(argv()))

    def test_root_in_the_container_is_refused(self):
        for bad in ["0:0", "0:10001", "10001:0"]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(uid_gid=bad)

    def test_a_malformed_user_is_refused(self):
        for bad in ["root", "10001", "10001:10001:0", "-1:5", ""]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(uid_gid=bad)

    def test_the_candidate_never_gets_group_add(self):
        # Only the handoff does; a candidate in qfclient could write into
        # artifacts/ directly and defeat the whole ownership dance.
        self.assertNotIn("--group-add", argv())

    def test_the_handoff_carries_group_add(self):
        a = argv(role="handoff", group_add=[10002])
        self.assertIn(("--group-add", "10002"), pairs(a))


class TestLabelsAndBothRoles(unittest.TestCase):
    def test_both_roles_carry_run_id_and_role(self):
        # Revision 7 labelled only the candidate, so a label-based "all
        # containers stopped" check could pass while the handoff still ran --
        # and forced cleanup depends entirely on that inventory.
        for role in sandbox.ROLES:
            with self.subTest(role=role):
                a = argv(role=role)
                p = pairs(a)
                self.assertIn(("--label", "qf.run_id=test-20260825T101112Z-"
                               "3f1c00000000-7"), p)
                self.assertIn(("--label", f"qf.role={role}"), p)
                self.assertIn(("--label", f"qf.spec_hash={'b' * 64}"), p)
                self.assertIn(("--label", "qf.kind=test"), p)
                self.assertIn(("--name", f"qf-test-20260825T101112Z-"
                               f"3f1c00000000-7-{role}"), p)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(sandbox.SandboxError):
            argv(role="builder")

    def test_a_run_id_that_is_unsafe_as_a_container_name_is_refused(self):
        for bad in ["", "../x", "a b", "a/b", "-leading", "x" * 200]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(run_id=bad)


class TestLimits(unittest.TestCase):
    def test_a_mem_limit_above_the_ceiling_raises_here_too(self):
        # A later caller must not be able to bypass spec.py.
        with self.assertRaises(sandbox.SandboxError):
            argv(mem_limit="32g")

    def test_a_malformed_mem_limit_raises(self):
        for bad in ["8G", "8", "", "8gb"]:
            with self.subTest(bad=bad), self.assertRaises(spec.SpecError):
                argv(mem_limit=bad)

    def test_a_nonpositive_or_non_numeric_cpu_count_raises(self):
        for bad in [0, -1, "4", True, None]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(cpus=bad)


class TestEntrypoint(unittest.TestCase):
    def test_entrypoint_elements_stay_separate(self):
        # An argv-to-shell collapse is how a validated field becomes a command.
        a = argv(entrypoint_argv=["/opt/qfenv/bin/python", "-m", "pytest",
                                  "-k", "hazard and not slow"])
        self.assertEqual(a[-5:], ["/opt/qfenv/bin/python", "-m", "pytest", "-k",
                                  "hazard and not slow"])

    def test_an_empty_or_non_list_entrypoint_raises(self):
        for bad in [[], "", None, "python", [1, 2]]:
            with self.subTest(bad=bad), self.assertRaises(sandbox.SandboxError):
                argv(entrypoint_argv=bad)

    def test_the_test_entrypoint_disables_the_cache_provider(self):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.assertEqual(
            sandbox.entrypoint_for(eff),
            ["/opt/qfenv/bin/python", "-m", "pytest", "-p", "no:cacheprovider",
             "-q", "trainer/tests"])

    def test_the_only_dash_p_comes_from_trusted_code(self):
        # -p loads plugins from the untrusted tree, so it is absent from the
        # spec allowlist and injected here instead.
        self.assertNotIn("-p", spec.PYTEST_FLAGS)
        for sneaky in ["-p", "-pno:randomly"]:
            with self.subTest(sneaky=sneaky), self.assertRaises(spec.SpecError):
                spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA,
                                "args": {"pytest_args": [sneaky]}})
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA,
                              "args": {"pytest_args": ["-q", "-x"]}})
        built = sandbox.entrypoint_for(eff)
        self.assertEqual([i for i, v in enumerate(built) if v == "-p"], [3])
        self.assertEqual(built[4], "no:cacheprovider")

    def test_the_test_entrypoint_carries_k_and_paths(self):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA,
                              "args": {"paths": ["tests/test_model.py"],
                                       "k": "hazard", "pytest_args": ["-q"]}})
        built = sandbox.entrypoint_for(eff)
        self.assertEqual(built[-3:], ["-k", "hazard", "tests/test_model.py"])

    def test_the_selftest_entrypoint_reads_from_the_trusted_mount(self):
        eff = spec.normalize({"schema": 1, "kind": "selftest",
                              "source_sha": SHA})
        self.assertEqual(sandbox.entrypoint_for(eff),
                         ["/bin/sh", "/trusted/nc13-inside.sh"])

    def test_an_unknown_kind_has_no_entrypoint(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.entrypoint_for({"kind": "confirm"})


class TestFlagOrder(unittest.TestCase):
    def test_the_argv_begins_with_docker_create(self):
        # `create`, not `run`. The two verbs are separate so that "the
        # container exists" is a fact the dispatcher can establish while it
        # still holds the phase gate; `docker run` cannot answer that question
        # until after it has already started the workload.
        self.assertEqual(argv()[:3], ["docker", "create", "--rm"])

    def test_mounts_come_after_the_limits_and_before_the_image(self):
        a = argv()
        self.assertLess(a.index("--memory"), a.index("-v"))
        self.assertLess(a.index("-v"), a.index(IMAGE))


class TestCreateThenStart(unittest.TestCase):
    """The start half carries no flags, so the create half must carry them all:
    a flag on `docker start` is silently ignored, which is how a boundary
    disappears without an error."""

    RUN = "test-20260825T101112Z-3f1c00000000-7"

    def test_the_start_argv_names_the_created_container(self):
        self.assertEqual(
            sandbox.docker_start_argv(self.RUN, "handoff"),
            ["docker", "start", "--attach", f"qf-{self.RUN}-handoff"])

    def test_the_created_name_is_the_name_that_is_started(self):
        created = argv(run_id=self.RUN, role="candidate")
        name = created[created.index("--name") + 1]
        self.assertEqual(sandbox.docker_start_argv(self.RUN, "candidate")[-1],
                         name)

    def test_no_tty_so_the_two_streams_stay_separate(self):
        # `docker start --attach` merges stdout and stderr if the container was
        # created with a TTY, and the bounded log writers keep them apart.
        self.assertNotIn("-t", argv())
        self.assertNotIn("--tty", argv())

    def test_an_unsafe_run_id_cannot_be_started(self):
        for bad in ("../x", "a b", "", "-lead"):
            with self.assertRaises(sandbox.SandboxError):
                sandbox.docker_start_argv(bad, "candidate")

    def test_an_unknown_role_cannot_be_started(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.docker_start_argv(self.RUN, "shell")


if __name__ == "__main__":
    unittest.main()


class TestTheExtractAndDataMounts(unittest.TestCase):
    """Phase 2b-2 Task 8. Two new mount destinations, and the direction each is
    allowed in is not the same -- which is the part the plan's outline did not
    say.

    `/extract` is a PUBLISHED, IMMUTABLE artifact (D20). A candidate that could
    write to it would corrupt the input to a recorded result, and the corruption
    would be invisible: the manifest's digests describe what was extracted, and
    nothing re-checks them before a later read. So it is read-only ONLY.

    `/app/trainer/data` is the opposite: `CACHE_DIR` and the model output path are
    computed relative to the module, so the trainer must be able to write there
    even though the tree around it is read-only. So it is read-write ONLY -- a
    read-only mount there would fail at the first cache write, deep inside pandas,
    with an error about a path nobody chose.
    """

    def argv(self, **over):
        kw = dict(image_ref="sha256:" + "a" * 64, run_id="r1",
                  spec_hash="h", kind="probe", src_mount="/srv/src",
                  out_mount="/srv/out",
                  entrypoint_argv=["/opt/qfenv/bin/python", "-c", "pass"],
                  mem_limit="4g", cpus=2.0)
        kw.update(over)
        return sandbox.docker_create_argv(**kw)

    def mounts(self, argv):
        return [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]

    def test_extract_mounts_read_only(self):
        argv = self.argv(extra_ro_mounts=[("/var/lib/qf-extracts/abc",
                                           sandbox.EXTRACT_DEST)])
        self.assertIn(f"/var/lib/qf-extracts/abc:{sandbox.EXTRACT_DEST}:ro",
                      self.mounts(argv))

    def test_extract_cannot_be_mounted_read_write(self):
        # The asymmetry that matters. A published extract is immutable, and a
        # candidate writing into it would corrupt the input to a recorded result.
        with self.assertRaises(sandbox.SandboxError) as cm:
            self.argv(extra_rw_mounts=[("/var/lib/qf-extracts/abc",
                                        sandbox.EXTRACT_DEST)])
        self.assertIn("read-only", str(cm.exception))

    def test_the_data_directory_mounts_read_write(self):
        argv = self.argv(extra_rw_mounts=[("/var/lib/qf-runs/r1/data",
                                           sandbox.DATA_DEST)])
        self.assertIn(f"/var/lib/qf-runs/r1/data:{sandbox.DATA_DEST}:rw",
                      self.mounts(argv))

    def test_the_data_directory_cannot_be_mounted_read_only(self):
        # It would fail at the first cache write, deep inside pandas, with an
        # error about a path nobody chose.
        with self.assertRaises(sandbox.SandboxError) as cm:
            self.argv(extra_ro_mounts=[("/var/lib/qf-runs/r1/data",
                                        sandbox.DATA_DEST)])
        self.assertIn("read-write", str(cm.exception))

    def test_the_data_mount_is_nested_inside_the_source_mount(self):
        # This is WHY 2b needs no path refactor in qf-research: CACHE_DIR is
        # module-relative, so the writable directory has to land inside the
        # read-only tree rather than beside it.
        self.assertTrue(sandbox.DATA_DEST.startswith(sandbox.SRC_DEST + "/"),
                        f"{sandbox.DATA_DEST} is not inside {sandbox.SRC_DEST}")

    def test_the_parent_mount_is_emitted_before_the_nested_one(self):
        # Docker orders bind mounts by destination depth, so nesting works either
        # way in practice. Emitting parent-first anyway means a reader does not
        # have to know that, and a later edit that reordered the argv would not
        # quietly depend on it.
        argv = self.argv(extra_rw_mounts=[("/var/lib/qf-runs/r1/data",
                                           sandbox.DATA_DEST)])
        mounts = self.mounts(argv)
        parent = [i for i, m in enumerate(mounts)
                  if m.split(":")[1] == sandbox.SRC_DEST][0]
        nested = [i for i, m in enumerate(mounts)
                  if m.split(":")[1] == sandbox.DATA_DEST][0]
        self.assertLess(parent, nested)

    def test_both_mounts_together_still_carry_the_whole_boundary(self):
        # A data plane that arrived by loosening the sandbox would be the
        # failure, so the flags NC13 asserts are re-asserted with the new mounts
        # present.
        argv = self.argv(
            extra_ro_mounts=[("/var/lib/qf-extracts/abc", sandbox.EXTRACT_DEST)],
            extra_rw_mounts=[("/var/lib/qf-runs/r1/data", sandbox.DATA_DEST)])
        for flag, value in (("--network", "none"), ("--cap-drop", "ALL"),
                            ("--user", sandbox.DEFAULT_UID_GID)):
            with self.subTest(flag=flag):
                self.assertIn(flag, argv)
                self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--read-only", argv)

    def test_no_database_url_is_ever_passed_in(self):
        # The candidate has no credential and the extract is how it gets data.
        argv = self.argv(
            extra_ro_mounts=[("/var/lib/qf-extracts/abc", sandbox.EXTRACT_DEST)],
            extra_rw_mounts=[("/var/lib/qf-runs/r1/data", sandbox.DATA_DEST)])
        self.assertNotIn("-e", argv)
        self.assertNotIn("--env", argv)
        for part in argv:
            self.assertNotIn("DATABASE_URL", part)

    def test_an_unallowlisted_destination_is_still_refused(self):
        for dest in ("/opt/qfenv", "/etc", "/app/trainer", "/",
                     "/extract/../etc", "/app/trainer/data/../.."):
            with self.subTest(dest=dest):
                with self.assertRaises(sandbox.SandboxError):
                    self.argv(extra_rw_mounts=[("/srv/x", dest)])

    def test_a_subdirectory_of_extract_is_not_allowlisted(self):
        # Exact destinations, not prefixes: mounting over `/extract/runs.parquet`
        # would replace one file of a manifest-described artifact and leave the
        # digest describing something else.
        with self.assertRaises(sandbox.SandboxError):
            self.argv(extra_ro_mounts=[("/srv/x",
                                        sandbox.EXTRACT_DEST + "/runs.parquet")])


class TestTheDataMountLandsWhereTheTrainerActuallyWrites(unittest.TestCase):
    """The container refused to start, verbatim:

        error mounting ".../data" to rootfs at "/app/trainer/data": create
        mountpoint for /app/trainer/data mount: mkdirat ...: read-only file system

    TWO defects, and the error named only the second. `DATA_DEST` was
    `/app/trainer/data`, one level short of where the trainer writes; and the
    mountpoint did not exist to mount onto.

    The layout: `extract-qf-research.sh` renames
    `tools/queue-forecasting/trainer/` to `trainer/`, and the WORKTREE ROOT is
    mounted at `SRC_DEST` -- which is why a `test` job's default path is
    `trainer/tests` and not `tests`. `CACHE_DIR` is `<module>/../data`, so it
    resolves to `SRC_DEST/trainer/data`."""

    def test_it_is_two_levels_down_not_one(self):
        self.assertEqual(sandbox.DATA_DEST, sandbox.SRC_DEST + "/trainer/data")

    def test_it_agrees_with_the_test_kinds_default_path(self):
        # Both facts come from the same layout: if the trainer package were at
        # the root, `trainer/tests` would be wrong too.
        import spec
        self.assertTrue(spec.DEFAULT_TEST_PATHS[0].startswith("trainer/"))
        self.assertTrue(
            sandbox.DATA_DEST[len(sandbox.SRC_DEST):].lstrip("/")
            .startswith("trainer/"))

    def test_the_relative_form_is_derivable_for_the_host_side(self):
        # The runner creates this directory in the worktree, and derives the
        # subpath from THIS constant rather than writing it out again -- two
        # literals for one location is how the mount and the directory came to
        # disagree.
        relative = sandbox.DATA_DEST[len(sandbox.SRC_DEST):].lstrip("/")
        self.assertEqual(relative, "trainer/data")

    def test_the_probe_entrypoint_is_absolute(self):
        # A relative path resolves against the image's WORKDIR -- a property of a
        # Dockerfile in another repository. If it moved, a probe would fail with
        # `can't open file` and nothing about mounts.
        argv = sandbox.entrypoint_for(
            {"kind": "probe",
             "args": {"path": "research/experiments/x.py", "extract": "a" * 64}})
        self.assertEqual(argv[1], sandbox.SRC_DEST + "/research/experiments/x.py")
        self.assertTrue(argv[1].startswith("/"))
