import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "environment_manager.py"
SPEC = importlib.util.spec_from_file_location("environment_manager", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeRunner:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.responses:
            return self.responses.pop(0)
        return subprocess.CompletedProcess(command, 0, "ok", "")


def response(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class EnvironmentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, **kwargs):
        return MODULE.EnvironmentManager(self.root, **kwargs)

    def test_resolve_is_read_only_and_lock_hash_is_stable(self):
        runner = FakeRunner()
        manager = self.manager(runner=runner)
        recipe = {
            "runtimes": ["r", "python"],
            "dependencies": [
                {"name": "DESeq2", "source": "bioconductor", "version": "1.50.0"},
                {"name": "scanpy", "source": "pypi", "version": "1.11.5"},
            ],
        }
        first = manager.resolve({"mode": "run"}, recipe)
        second = manager.resolve({"mode": "run"}, recipe)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual([x.lock_hash for x in first.environments], [x.lock_hash for x in second.environments])
        self.assertEqual({x.runtime for x in first.environments}, {"r", "python"})
        self.assertFalse(manager.cache_root.exists())
        self.assertEqual(runner.calls, [])

        permuted = manager.resolve(
            {"mode": "run"},
            {
                "runtimes": ["python", "r"],
                "dependencies": list(reversed(recipe["dependencies"])),
            },
        )
        self.assertEqual(first.plan_id, permuted.plan_id)

    def test_authorization_gate_blocks_provision_without_writes(self):
        manager = self.manager(runner=FakeRunner())
        plan = manager.resolve(
            {"mode": "run"},
            {"runtimes": ["python"], "dependencies": [{"name": "scanpy", "source": "pypi"}]},
        )
        with self.assertRaises(MODULE.AuthorizationError):
            manager.provision(plan, MODULE.ExecutionAuthorization(mode="plan", approved=False))
        self.assertFalse(manager.cache_root.exists())

    def test_plan_mode_cannot_provision_even_with_authorization(self):
        manager = self.manager(runner=FakeRunner())
        plan = manager.resolve({"mode": "plan"}, {"runtimes": ["python"]})
        with self.assertRaises(MODULE.AuthorizationError):
            manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))

    def test_github_requires_allowlist_and_full_sha(self):
        dependency = {
            "name": "mypkg",
            "source": "github",
            "repository": "owner/repo",
            "ref": "main",
            "runtime": "python",
        }
        manager = self.manager(github_allowlist=["owner/repo"])
        with self.assertRaisesRegex(MODULE.PolicyError, "40-character"):
            manager.resolve({"mode": "run"}, {"runtimes": ["python"], "dependencies": [dependency]})
        dependency["ref"] = "a" * 40
        manager.resolve({"mode": "run"}, {"runtimes": ["python"], "dependencies": [dependency]})
        blocked = self.manager(github_allowlist=[])
        with self.assertRaisesRegex(MODULE.PolicyError, "not allowlisted"):
            blocked.resolve({"mode": "run"}, {"runtimes": ["python"], "dependencies": [dependency]})

    def test_cache_root_cannot_escape_task_root(self):
        with self.assertRaises(MODULE.PolicyError):
            MODULE.EnvironmentManager(self.root, cache_root=self.root.parent / "outside")

    def test_short_cache_key_preserves_full_lock_identity(self):
        manager = self.manager(runner=FakeRunner(), cache_key_chars=24)
        plan = manager.resolve(
            {"mode": "run"},
            {"runtimes": ["r"], "dependencies": [{"name": "Rcpp", "version": "1.1.1"}]},
        )
        spec = plan.environments[0]
        self.assertRegex(spec.lock_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(Path(spec.path).parent.name, spec.lock_hash[:24])
        self.assertEqual(plan.to_dict()["environments"][0]["lock_hash"], spec.lock_hash)
        self.assertEqual(manager.probe()["policy"]["cache_key_chars"], 24)

    def test_short_cache_key_has_reviewed_collision_floor(self):
        with self.assertRaisesRegex(ValueError, "between 16 and 64"):
            self.manager(cache_key_chars=12)

    def test_probe_detects_r_alias_and_device_guard_read_only(self):
        runner = FakeRunner(
            [
                response(stdout="Invoke-History\n"),
                response(code=3, stderr="This program is blocked by group policy"),
            ]
        )
        manager = self.manager(runner=runner)
        original_which = MODULE.shutil.which
        MODULE.shutil.which = lambda name: "powershell.exe" if name == "powershell" else None
        try:
            result = manager.probe([self.root / "blocked.exe"])
        finally:
            MODULE.shutil.which = original_which
        self.assertTrue(result["read_only"])
        self.assertTrue(result["r"]["alias_collision"])
        self.assertEqual(result["r"]["powershell_r_alias"], "Invoke-History")
        self.assertTrue(result["candidate_executables"][0]["device_guard_blocked"])

    def test_provision_retries_at_most_twice_and_reports_failure(self):
        runner = FakeRunner([response(1, stderr="connection timed out"), response(1, stderr="connection timed out")])
        manager = self.manager(runner=runner, max_attempts=2)
        plan = manager.resolve(
            {"mode": "run"},
            {"runtimes": ["python"], "dependencies": [{"name": "scanpy", "source": "pypi"}]},
        )
        with self.assertRaises(MODULE.ProvisionError) as raised:
            manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(raised.exception.report["attempts"], 2)
        self.assertEqual(raised.exception.report["failure_class"], "NETWORK")
        self.assertFalse(raised.exception.report["global_changes"])

    def test_offline_cache_miss_is_precise_non_retryable_and_sanitized(self):
        private_path = "".join(("C:", "\\", "Users", "\\", "secret", "\\", "cache"))
        stderr = (
            "Network connectivity is disabled, and llvmlite==0.48.0 wasn't found in the cache for "
            f"https://files.example.invalid/llvmlite.whl at {private_path} token=abc123"
        )
        runner = FakeRunner([response(1, stderr=stderr), response(1, stderr=stderr)])
        manager = self.manager(runner=runner, max_attempts=2)
        plan = manager.resolve(
            {"mode": "run"},
            {"runtimes": ["python"], "dependencies": [{"name": "scanpy", "source": "pypi"}]},
        )
        with self.assertRaises(MODULE.ProvisionError) as raised:
            manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))
        report = raised.exception.report
        self.assertEqual(report["failure_class"], "OFFLINE_CACHE_MISS")
        self.assertFalse(report["retry_safe"])
        self.assertIn("llvmlite==0.48.0", report["error_summary"])
        self.assertNotIn("https://", report["error_summary"])
        self.assertNotIn(private_path, report["error_summary"])
        self.assertNotIn("abc123", report["error_summary"])

    def test_failure_command_outline_redacts_locators_urls_and_credentials(self):
        private_python = str(Path("C:" + "\\" + "Users" + "\\" + "Example" + "\\" + "Private Project" + "\\" + "python.exe"))
        private_cache = "D:" + "\\" + "secret" + "\\" + "uv-cache"
        unc_cache = "\\" + "\\" + "private-server" + "\\" + "share" + "\\" + "cache"
        posix_cache = "/" + "/".join(("home", "example", "private-cache"))
        generic_posix_cache = "/" + "/".join(("data", "private", "cache"))
        file_uri = "file:" + "/" * 3 + "C:" + "/" + "/".join(("Users", "Example", "cache"))
        manager = self.manager()
        plan = manager.resolve(
            {"mode": "run"},
            {"runtimes": ["python"], "dependencies": [{"name": "scanpy", "source": "pypi"}]},
        )
        report = manager.report_failure(
            plan.environments[0],
            stage="provision",
            command_record={
                "command": [
                    private_python,
                    "--cache-dir",
                    private_cache,
                    unc_cache,
                    posix_cache,
                    generic_posix_cache,
                    file_uri,
                    "https://packages.example.invalid/simple",
                    "token=abc123",
                ],
                "returncode": 1,
                "stdout": "",
                "stderr": "failed",
            },
            attempts=1,
        )
        command_text = " ".join(report["command"])
        for secret in (
            private_python,
            private_cache,
            unc_cache,
            posix_cache,
            generic_posix_cache,
            file_uri,
            "https://",
            "abc123",
        ):
            self.assertNotIn(secret, command_text)
        self.assertGreaterEqual(command_text.count("<path-redacted>"), 5)
        self.assertGreaterEqual(command_text.count("<url-redacted>"), 2)
        self.assertRegex(report["command_fingerprint"], r"^[0-9a-f]{16}$")

        error_summary = manager._redact_error_summary(
            "failed " + " ".join((private_python, generic_posix_cache, file_uri))
        )
        for locator in (private_python, generic_posix_cache, file_uri, "file:///"):
            self.assertNotIn(locator, error_summary)

    def test_failed_download_is_classified_as_network(self):
        for message in (
            "Failed to download wheel: network is unreachable",
            "urllib3.exceptions.ReadTimeoutError: ReadTimeout",
            "http.client.IncompleteRead: IncompleteRead(1024 bytes read)",
        ):
            with self.subTest(message=message):
                self.assertEqual(MODULE.EnvironmentManager._classify_failure(message), "NETWORK")

    def test_device_guard_failure_quarantines(self):
        runner = FakeRunner(
            [
                response(0, stdout="created"),
                response(3, stderr="Windows Defender Application Control policy blocked this app"),
            ]
        )
        manager = self.manager(runner=runner, max_attempts=1)
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["python"]})
        handles = manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))
        handle = handles[0]
        with self.assertRaises(MODULE.ProvisionError) as raised:
            manager.verify(handle)
        self.assertEqual(handle.state, "quarantined")
        self.assertEqual(raised.exception.report["failure_class"], "DEVICE_GUARD")

    def test_freeze_requires_verification_and_writes_task_local_marker(self):
        runner = FakeRunner()
        manager = self.manager(runner=runner)
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["python"]})
        handle = manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))[0]
        with self.assertRaises(MODULE.PolicyError):
            manager.freeze(handle, MODULE.ExecutionAuthorization(mode="run", approved=True))
        manager.verify(handle)
        marker = manager.freeze(handle, MODULE.ExecutionAuthorization(mode="run", approved=True))
        marker_path = Path(handle.spec.path) / "environment.locked.json"
        self.assertTrue(marker_path.is_file())
        self.assertEqual(json.loads(marker_path.read_text())["lock_hash"], handle.spec.lock_hash)
        self.assertEqual(marker["env_id"], handle.spec.env_id)
        self.assertEqual((Path(handle.spec.path) / "requirements.lock.txt").read_text(), "ok")

    def test_execute_r_uses_absolute_rscript_and_process_local_path(self):
        runner = FakeRunner()
        rscript = self.root / "R" / "bin" / "Rscript.exe"
        manager = self.manager(runner=runner, rscript=rscript)
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["r"]})
        spec = plan.environments[0]
        Path(spec.path).mkdir(parents=True)
        handle = MODULE.EnvironmentHandle(spec=spec, state="frozen", verified=True, frozen=True)
        script = self.root / "analysis.R"
        script.write_text("print('ok')", encoding="utf-8")
        result = manager.execute(
            handle,
            script,
            MODULE.ExecutionAuthorization(mode="run", approved=True),
        )
        self.assertEqual(result.returncode, 0)
        command, kwargs = runner.calls[-1]
        self.assertEqual(command[0], str(rscript))
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertIn("renv::load", command[3])
        self.assertIn("sys.source", command[3])
        self.assertNotIn("--args", command)
        self.assertIn("env", kwargs)

    def test_reverify_preserves_frozen_state(self):
        manager = self.manager(runner=FakeRunner())
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["python"]})
        spec = plan.environments[0]
        Path(spec.path).mkdir(parents=True)
        handle = MODULE.EnvironmentHandle(spec=spec, state="frozen", verified=True, frozen=True)
        manager.verify(handle)
        self.assertTrue(handle.verified)
        self.assertTrue(handle.frozen)
        self.assertEqual(handle.state, "frozen")

    def test_access_violation_has_native_process_crash_failure_class(self):
        manager = self.manager()
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["r"]})
        report = manager.report_failure(
            plan.environments[0],
            stage="provision",
            command_record={
                "command": [str(manager.rscript), "--vanilla", "-e", "cat('done')"],
                "returncode": 3221225477,
                "stdout": "package installation completed",
                "stderr": "",
            },
            attempts=2,
        )
        self.assertEqual(report["failure_class"], "NATIVE_PROCESS_CRASH")
        self.assertEqual(report["diagnostic_code"], "ACCESS_VIOLATION_0xC0000005")
        self.assertFalse(report["retry_safe"])

    def test_r_renv_provisions_into_platform_project_library_with_pak(self):
        manager = self.manager(runner=FakeRunner())
        plan = manager.resolve(
            {"mode": "run"},
            {
                "runtimes": ["r"],
                "backend": "r-renv",
                "dependencies": [
                    {"name": "crayon", "source": "cran", "version": "1.5.3", "runtime": "r"}
                ],
            },
        )
        command = manager._provision_commands(plan.environments[0])[0]
        self.assertEqual(command[0], str(manager.rscript))
        self.assertIn("renv_ns$paths$library", command[3])
        self.assertIn("pak_ns$pkg_install", command[3])
        self.assertIn("crayon@1.5.3", command[3])
        self.assertNotIn("'renv','library'", command[3])
        self.assertIn("environment-requirements.R", command[3])
        freeze = manager._freeze_command(plan.environments[0])
        self.assertIn("renv::lockfile_read", freeze[3])
        self.assertIn("lock$Packages", freeze[3])
        self.assertIn("packageVersion", freeze[3])
        self.assertNotIn("renv::status", freeze[3])

        verify = manager._verification_command(plan.environments[0])
        self.assertIn("renv::lockfile_read", verify[3])
        self.assertIn("installed_versions", verify[3])
        self.assertNotIn("renv::status", verify[3])

    def test_r_pins_first_strategy_is_locked_and_compiles_sequential_pak_calls(self):
        manager = self.manager(runner=FakeRunner())
        recipe = {
            "runtimes": ["r"],
            "backend": "r-renv",
            "r_install_strategy": "pins-first",
            "r_preinstall": ["Rcpp"],
            "dependencies": [
                {"name": "DEP", "source": "bioconductor", "version": "1.32.0", "runtime": "r"},
                {"name": "Rcpp", "source": "cran", "version": "1.1.1", "runtime": "r"},
            ],
        }
        plan = manager.resolve({"mode": "run"}, recipe)
        spec = plan.environments[0]
        self.assertEqual(spec.install_strategy, "pins-first")
        self.assertEqual(spec.preinstall, ("Rcpp",))
        command = manager._provision_commands(spec)[0][3]
        first = command.index("Rcpp@1.1.1")
        second = command.index("bioc::DEP@1.32.0")
        self.assertLess(first, second)
        self.assertEqual(command.count("pak_ns$pkg_install"), 2)
        self.assertEqual(command.count("upgrade=FALSE"), 2)

        simultaneous = dict(recipe)
        simultaneous.pop("r_install_strategy")
        simultaneous.pop("r_preinstall")
        other = manager.resolve({"mode": "run"}, simultaneous)
        self.assertNotEqual(spec.lock_hash, other.environments[0].lock_hash)

    def test_pins_first_rejects_unlocked_or_unknown_preinstall(self):
        manager = self.manager(runner=FakeRunner())
        base = {
            "runtimes": ["r"],
            "r_install_strategy": "pins-first",
            "r_preinstall": ["Rcpp"],
            "dependencies": [
                {"name": "Rcpp", "source": "cran", "runtime": "r"},
            ],
        }
        with self.assertRaisesRegex(MODULE.PolicyError, "exact versions"):
            manager.resolve({"mode": "run"}, base)
        unknown = dict(base)
        unknown["dependencies"] = [
            {"name": "DEP", "source": "bioconductor", "version": "1.32.0", "runtime": "r"}
        ]
        with self.assertRaisesRegex(MODULE.PolicyError, "declared"):
            manager.resolve({"mode": "run"}, unknown)

    def test_task_local_windows_exit_helper_is_part_of_r_lock_and_command(self):
        helper = self.root / "runtime" / "hard_exit.dll"
        helper.parent.mkdir()
        helper.write_bytes(b"fixture-dll")
        manager = self.manager(runner=FakeRunner(), windows_exit_helper=helper)
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["r"]})
        command = manager._provision_commands(plan.environments[0])[0]
        self.assertIn("biomedical_environment_terminate_process", command[3])
        self.assertIn("provision.complete", command[3])
        self.assertEqual(
            plan.environments[0].lock_hash,
            manager.resolve({"mode": "run"}, {"runtimes": ["r"]}).environments[0].lock_hash,
        )

    def test_cached_environment_requires_exact_lock_and_platform(self):
        runner = FakeRunner()
        manager = self.manager(runner=runner)
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["python"]})
        spec = plan.environments[0]
        env_dir = Path(spec.path)
        env_dir.mkdir(parents=True)
        lock = env_dir / "requirements.lock.txt"
        lock.write_text("frozen\n", encoding="utf-8")
        (env_dir / "environment.locked.json").write_text(
            json.dumps(
                {
                    "lock_hash": spec.lock_hash,
                    "platform": plan.platform,
                    "backend_lock": {
                        "path": "requirements.lock.txt",
                        "sha256": manager._sha256_file(lock),
                    },
                }
            ),
            encoding="utf-8",
        )
        handle = manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))[0]
        self.assertTrue(handle.frozen)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("-c", runner.calls[0][0])

    def test_cached_environment_rejects_tampered_backend_lock(self):
        runner = FakeRunner()
        manager = self.manager(runner=runner)
        plan = manager.resolve({"mode": "run"}, {"runtimes": ["python"]})
        spec = plan.environments[0]
        env_dir = Path(spec.path)
        env_dir.mkdir(parents=True)
        lock = env_dir / "requirements.lock.txt"
        lock.write_text("tampered\n", encoding="utf-8")
        (env_dir / "environment.locked.json").write_text(
            json.dumps(
                {
                    "lock_hash": spec.lock_hash,
                    "platform": plan.platform,
                    "backend_lock": {
                        "path": "requirements.lock.txt",
                        "sha256": "0" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )
        handle = manager.provision(plan, MODULE.ExecutionAuthorization(mode="run", approved=True))[0]
        self.assertFalse(handle.frozen)
        self.assertEqual(handle.state, "provisioned")
        self.assertEqual(runner.calls[0][0][1], "venv")


if __name__ == "__main__":
    unittest.main()
