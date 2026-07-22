"""Task-local, reproducible environment management for biomedical workflows.

The module deliberately separates dependency declaration from provisioning.  Calling
``probe`` or ``resolve`` never changes an environment.  Network access and installs
require an explicit :class:`ExecutionAuthorization` with ``mode='run'``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


DEFAULT_RSCRIPT = Path(r"C:\Program Files\R\R-4.5.3\bin\Rscript.exe")
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
DEVICE_GUARD_PATTERNS = (
    "device guard",
    "application control policy",
    "windows defender application control",
    "this program is blocked by group policy",
    "this app has been blocked by your system administrator",
    "applocker",
    "0x800704ec",
)


class EnvironmentManagerError(RuntimeError):
    """Base error for an environment lifecycle violation."""


class AuthorizationError(EnvironmentManagerError):
    """Raised when a mutating operation lacks explicit run authorization."""


class PolicyError(EnvironmentManagerError):
    """Raised when a plan would mutate global state or use an unsafe source."""


class ProvisionError(EnvironmentManagerError):
    """Raised when a task-local environment cannot be provisioned."""

    def __init__(self, message: str, report: Mapping[str, Any]):
        super().__init__(message)
        self.report = dict(report)


@dataclass(frozen=True)
class ExecutionAuthorization:
    """Explicit authority for a task-local execution.

    Global/base environments, system PATH, administrator changes and unpinned URLs
    are intentionally not representable by this object.
    """

    mode: str
    approved: bool
    allow_network_install: bool = True
    allowed_sources: tuple[str, ...] = (
        "cran",
        "bioconductor",
        "pypi",
        "conda-forge",
        "bioconda",
        "github",
    )

    def require_run(self, *, network: bool = False) -> None:
        if self.mode != "run" or not self.approved:
            raise AuthorizationError("Operation requires explicit approved mode='run'.")
        if network and not self.allow_network_install:
            raise AuthorizationError("Authorization does not permit network installation.")


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str | None = None
    source: str = "cran"
    repository: str | None = None
    ref: str | None = None
    runtime: str | None = None
    conda_name: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Dependency":
        return cls(
            name=str(value["name"]),
            version=str(value["version"]) if value.get("version") else None,
            source=str(value.get("source", "cran")).lower(),
            repository=str(value["repository"]) if value.get("repository") else None,
            ref=str(value["ref"]) if value.get("ref") else None,
            runtime=str(value["runtime"]).lower() if value.get("runtime") else None,
            conda_name=str(value["conda_name"]) if value.get("conda_name") else None,
        )


@dataclass(frozen=True)
class EnvironmentSpec:
    env_id: str
    runtime: str
    backend: str
    path: str
    dependencies: tuple[Dependency, ...]
    lock_hash: str
    install_strategy: str = "simultaneous"
    preinstall: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentPlan:
    plan_id: str
    mode: str
    task_root: str
    cache_root: str
    platform: str
    environments: tuple[EnvironmentSpec, ...]
    state: str = "planned"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnvironmentHandle:
    spec: EnvironmentSpec
    state: str = "provisioning"
    verified: bool = False
    frozen: bool = False
    quarantine_reason: str | None = None
    command_log: list[dict[str, Any]] = field(default_factory=list)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner(
    command: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        timeout=timeout,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
    )


class EnvironmentManager:
    """Resolve and operate isolated R/Python environments inside a managed root."""

    def __init__(
        self,
        task_root: str | os.PathLike[str],
        *,
        cache_root: str | os.PathLike[str] | None = None,
        rscript: str | os.PathLike[str] = DEFAULT_RSCRIPT,
        github_allowlist: Iterable[str] = (),
        windows_exit_helper: str | os.PathLike[str] | None = None,
        runner: Runner | None = None,
        max_attempts: int = 2,
        cache_key_chars: int = 64,
    ) -> None:
        self.task_root = Path(task_root).expanduser().resolve()
        self.cache_root = Path(cache_root or self.task_root / ".environment-cache").expanduser().resolve()
        self.rscript = Path(rscript).expanduser()
        self.github_allowlist = {self._normalise_repo(repo) for repo in github_allowlist}
        self.windows_exit_helper = (
            Path(windows_exit_helper).expanduser().resolve() if windows_exit_helper else None
        )
        if self.windows_exit_helper is not None:
            self._assert_managed_path(self.windows_exit_helper)
            if self.windows_exit_helper.suffix.lower() != ".dll":
                raise PolicyError("Windows R exit helper must be a task-local DLL.")
        self.runner = runner or _default_runner
        if max_attempts < 1 or max_attempts > 2:
            raise ValueError("max_attempts must be 1 or 2")
        self.max_attempts = max_attempts
        if cache_key_chars < 16 or cache_key_chars > 64:
            raise ValueError("cache_key_chars must be between 16 and 64")
        self.cache_key_chars = cache_key_chars
        self._assert_managed_path(self.cache_root)

    # ---------- discovery and planning (strictly read-only) ----------

    def probe(self, candidate_executables: Iterable[str | os.PathLike[str]] = ()) -> dict[str, Any]:
        """Return a read-only runtime inventory and policy warnings."""

        powershell = shutil.which("pwsh") or shutil.which("powershell")
        r_alias: str | None = None
        if powershell:
            result = self.runner(
                [powershell, "-NoProfile", "-Command", "(Get-Alias R -ErrorAction SilentlyContinue).Definition"],
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                r_alias = result.stdout.strip()

        candidates: list[dict[str, Any]] = []
        for raw_path in candidate_executables:
            executable = Path(raw_path).expanduser()
            result = self.runner([str(executable), "--version"], timeout=20)
            combined = f"{result.stdout}\n{result.stderr}"
            candidates.append(
                {
                    "path": str(executable),
                    "returncode": result.returncode,
                    "device_guard_blocked": self._is_device_guard(combined),
                    "stderr_fingerprint": self._fingerprint(result.stderr),
                }
            )

        return {
            "state": "discovered",
            "read_only": True,
            "platform": self._platform_key(),
            "r": {
                "rscript": str(self.rscript),
                "exists": self.rscript.is_file(),
                "powershell_r_alias": r_alias,
                "alias_collision": bool(r_alias),
            },
            "python": {"executable": sys.executable, "version": platform.python_version()},
            "tools": {
                name: shutil.which(name)
                for name in ("uv", "conda", "git", "make", "gcc", "g++")
            },
            "candidate_executables": candidates,
            "policy": {
                "global_mutation": False,
                "r_invocation": "absolute-rscript-only",
                "device_guard_blocked_candidates_are_quarantined": True,
                "windows_exit_workaround_configured": self.windows_exit_helper is not None,
                "cache_key_chars": self.cache_key_chars,
            },
        }

    def resolve(
        self,
        intent: Mapping[str, Any],
        recipe: Mapping[str, Any],
        probe_result: Mapping[str, Any] | None = None,
    ) -> EnvironmentPlan:
        """Compile a deterministic task-local plan without creating directories."""

        mode = str(intent.get("mode", "plan"))
        if mode not in {"plan", "run", "resume", "reproduce-figure", "explain"}:
            raise PolicyError(f"Unsupported mode: {mode}")
        runtimes = tuple(dict.fromkeys(str(v).lower() for v in recipe.get("runtimes", ())))
        if not runtimes:
            runtimes = self._infer_runtimes(recipe.get("dependencies", ()))
        if not runtimes or any(runtime not in {"r", "python"} for runtime in runtimes):
            raise PolicyError("Recipe runtimes must contain only 'r' and/or 'python'.")
        runtimes = tuple(sorted(runtimes, key={"r": 0, "python": 1}.get))

        dependencies = tuple(Dependency.from_mapping(item) for item in recipe.get("dependencies", ()))
        for dependency in dependencies:
            self._validate_dependency(dependency)

        specs: list[EnvironmentSpec] = []
        platform_key = str((probe_result or {}).get("platform") or self._platform_key())
        for runtime in runtimes:
            runtime_deps = tuple(
                sorted(
                    (dep for dep in dependencies if self._dependency_runtime(dep) == runtime),
                    key=lambda dep: (
                        dep.source,
                        dep.name.lower(),
                        dep.version or "",
                        dep.repository or "",
                        dep.ref or "",
                    ),
                )
            )
            backend = self._select_backend(runtime, recipe, runtime_deps)
            install_strategy = str(
                recipe.get(f"{runtime}_install_strategy", recipe.get("install_strategy", "simultaneous"))
            ).strip().lower()
            if install_strategy not in {"simultaneous", "pins-first"}:
                raise PolicyError(f"Unsupported install strategy: {install_strategy}")
            preinstall_raw = recipe.get(f"{runtime}_preinstall", recipe.get("preinstall", ()))
            if isinstance(preinstall_raw, str):
                preinstall_raw = (preinstall_raw,)
            preinstall = tuple(
                sorted(
                    dict.fromkeys(str(name).strip() for name in preinstall_raw if str(name).strip()),
                    key=str.lower,
                )
            )
            dependency_names = {dependency.name.lower(): dependency.name for dependency in runtime_deps}
            unknown_preinstall = [name for name in preinstall if name.lower() not in dependency_names]
            if unknown_preinstall:
                raise PolicyError(
                    "Preinstall dependencies must also be declared in the locked dependency set: "
                    + ", ".join(unknown_preinstall)
                )
            if install_strategy == "pins-first":
                if runtime != "r" or backend != "r-renv":
                    raise PolicyError("The pins-first strategy is supported only for task-local r-renv environments.")
                if not preinstall:
                    raise PolicyError("The pins-first strategy requires at least one r_preinstall package.")
                preinstall_names = {name.lower() for name in preinstall}
                unpinned = [
                    dependency.name
                    for dependency in runtime_deps
                    if dependency.name.lower() in preinstall_names and not dependency.version
                ]
                if unpinned:
                    raise PolicyError(
                        "Pins-first dependencies require exact versions: " + ", ".join(unpinned)
                    )
            elif preinstall:
                raise PolicyError("r_preinstall is only valid with install_strategy='pins-first'.")
            if backend == "conda":
                for dependency in runtime_deps:
                    if dependency.source == "github":
                        raise PolicyError("Pinned GitHub packages require a separate pak/uv environment, not Conda.")
                    if dependency.source not in {"conda-forge", "bioconda"} and not dependency.conda_name:
                        raise PolicyError(
                            f"Conda fallback requires conda_name mapping for {dependency.name}."
                        )
            canonical = {
                "runtime": runtime,
                "backend": backend,
                "platform": platform_key,
                "dependencies": [asdict(dep) for dep in runtime_deps],
                "runtime_version": recipe.get(f"{runtime}_version"),
                "install_strategy": install_strategy,
                "preinstall": list(preinstall),
                "windows_exit_workaround": (
                    {
                        "enabled": True,
                        "helper_sha256": self._sha256_file(self.windows_exit_helper),
                    }
                    if runtime == "r"
                    and self.windows_exit_helper is not None
                    and self.windows_exit_helper.is_file()
                    else {"enabled": False}
                ),
            }
            lock_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            env_path = (self.cache_root / lock_hash[: self.cache_key_chars] / runtime).resolve()
            self._assert_managed_path(env_path)
            specs.append(
                EnvironmentSpec(
                    env_id=f"env_{runtime}_{lock_hash[:12]}",
                    runtime=runtime,
                    backend=backend,
                    path=str(env_path),
                    dependencies=runtime_deps,
                    lock_hash=lock_hash,
                    install_strategy=install_strategy,
                    preinstall=preinstall,
                )
            )

        plan_payload = {
            "mode": mode,
            "task_root": str(self.task_root),
            "platform": platform_key,
            "environments": [asdict(spec) for spec in specs],
        }
        plan_id = hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return EnvironmentPlan(
            plan_id=plan_id,
            mode=mode,
            task_root=str(self.task_root),
            cache_root=str(self.cache_root),
            platform=platform_key,
            environments=tuple(specs),
        )

    # ---------- authorized task-local mutations ----------

    def provision(
        self,
        plan: EnvironmentPlan,
        authorization: ExecutionAuthorization,
    ) -> tuple[EnvironmentHandle, ...]:
        """Provision each planned environment after an explicit authorization gate."""

        authorization.require_run(network=any(spec.dependencies for spec in plan.environments))
        if plan.mode not in {"run", "resume", "reproduce-figure"}:
            raise AuthorizationError(f"Plan mode '{plan.mode}' cannot provision environments.")
        self._validate_plan_roots(plan)
        handles: list[EnvironmentHandle] = []
        for spec in plan.environments:
            for dependency in spec.dependencies:
                if dependency.source not in authorization.allowed_sources:
                    raise AuthorizationError(f"Source is not authorized: {dependency.source}")
            env_dir = Path(spec.path)
            env_dir.mkdir(parents=True, exist_ok=True)
            handle = EnvironmentHandle(spec=spec)
            cached_marker = env_dir / "environment.locked.json"
            if cached_marker.is_file():
                try:
                    marker = json.loads(cached_marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    marker = {}
                if (
                    marker.get("lock_hash") == spec.lock_hash
                    and marker.get("platform") == plan.platform
                    and self._cached_backend_lock_matches(spec, marker)
                ):
                    handle.state = "provisioned"
                    self.verify(handle)
                    handle.frozen = True
                    handle.state = "frozen"
                    handles.append(handle)
                    continue

            commands = self._provision_commands(spec)
            for command in commands:
                self._clear_shutdown_marker(spec, "provision")
                record = self._run_with_retry(command, cwd=str(env_dir))
                handle.command_log.append(record)
                if record["returncode"] != 0:
                    report = self.report_failure(
                        spec,
                        stage="provision",
                        command_record=record,
                        attempts=record["attempts"],
                    )
                    if report["failure_class"] == "DEVICE_GUARD":
                        handle.state = "quarantined"
                        handle.quarantine_reason = "DEVICE_GUARD"
                    raise ProvisionError(f"Provisioning failed for {spec.env_id}", report)
                self._require_shutdown_marker(spec, "provision")
            handle.state = "provisioned"
            handles.append(handle)
        return tuple(handles)

    def verify(self, handle: EnvironmentHandle) -> dict[str, Any]:
        """Run backend-specific load checks without installing or changing dependencies."""

        if handle.state == "quarantined":
            raise PolicyError("Quarantined environments cannot be verified.")
        was_frozen = handle.frozen and handle.state == "frozen"
        command = self._verification_command(handle.spec)
        self._clear_shutdown_marker(handle.spec, "verify")
        record = self._run_with_retry(command, cwd=handle.spec.path, attempts=1)
        handle.command_log.append(record)
        if record["returncode"] != 0:
            report = self.report_failure(
                handle.spec, stage="verify", command_record=record, attempts=1
            )
            if report["failure_class"] == "DEVICE_GUARD":
                handle.state = "quarantined"
                handle.quarantine_reason = "DEVICE_GUARD"
            else:
                handle.state = "failed"
            raise ProvisionError(f"Verification failed for {handle.spec.env_id}", report)
        self._require_shutdown_marker(handle.spec, "verify")
        handle.verified = True
        handle.state = "frozen" if was_frozen else "verified"
        return {"env_id": handle.spec.env_id, "verified": True, "record": record}

    def freeze(
        self,
        handle: EnvironmentHandle,
        authorization: ExecutionAuthorization,
    ) -> dict[str, Any]:
        """Write task-local lock evidence after successful verification."""

        authorization.require_run()
        if not handle.verified:
            raise PolicyError("Environment must pass verify() before freeze().")
        env_dir = Path(handle.spec.path)
        self._assert_managed_path(env_dir)
        command = self._freeze_command(handle.spec)
        self._clear_shutdown_marker(handle.spec, "freeze")
        record = self._run_with_retry(command, cwd=str(env_dir), attempts=1)
        handle.command_log.append(record)
        if record["returncode"] != 0:
            raise ProvisionError(
                f"Freeze failed for {handle.spec.env_id}",
                self.report_failure(handle.spec, stage="freeze", command_record=record, attempts=1),
            )
        self._require_shutdown_marker(handle.spec, "freeze")
        if handle.spec.backend == "python-uv":
            (env_dir / "requirements.lock.txt").write_text(record["stdout"], encoding="utf-8")
        elif handle.spec.backend == "conda":
            (env_dir / "explicit.txt").write_text(record["stdout"], encoding="utf-8")
        backend_lock = self._backend_lock_path(handle.spec)
        if not backend_lock.is_file():
            missing_record = {
                "command": record.get("command", ()),
                "returncode": 1,
                "stdout": record.get("stdout", ""),
                "stderr": f"Backend lock was not created: {backend_lock.name}",
            }
            raise ProvisionError(
                f"Freeze did not create a backend lock for {handle.spec.env_id}",
                self.report_failure(
                    handle.spec,
                    stage="freeze",
                    command_record=missing_record,
                    attempts=1,
                ),
            )
        marker = {
            "env_id": handle.spec.env_id,
            "lock_hash": handle.spec.lock_hash,
            "platform": self._platform_key(),
            "backend": handle.spec.backend,
            "backend_lock": {
                "path": backend_lock.relative_to(env_dir).as_posix(),
                "sha256": self._sha256_file(backend_lock),
            },
            "windows_exit_workaround": self._windows_exit_evidence(),
            "frozen_at": datetime.now(timezone.utc).isoformat(),
        }
        (env_dir / "environment.locked.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        handle.frozen = True
        handle.state = "frozen"
        return marker

    def execute(
        self,
        handle: EnvironmentHandle,
        script: str | os.PathLike[str],
        authorization: ExecutionAuthorization,
        *,
        args: Sequence[str] = (),
        cwd: str | os.PathLike[str] | None = None,
        timeout: int = 86_400,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a script with a frozen interpreter and process-local environment."""

        authorization.require_run()
        if not handle.frozen or handle.state != "frozen":
            raise PolicyError("Only a verified, frozen environment can execute analyses.")
        script_path = Path(script).expanduser().resolve()
        if not script_path.is_file():
            raise FileNotFoundError(script_path)
        workdir = Path(cwd or self.task_root).expanduser().resolve()
        self._assert_managed_path(workdir, allow_task_root=True)
        command = self._execution_command(handle.spec, script_path, args)
        process_env = os.environ.copy()
        if handle.spec.runtime == "r":
            rtools = Path(r"C:\rtools45\usr\bin")
            if rtools.is_dir():
                process_env["PATH"] = f"{rtools}{os.pathsep}{process_env.get('PATH', '')}"
        self._clear_shutdown_marker(handle.spec, "execute")
        result = self.runner(command, cwd=str(workdir), env=process_env, timeout=timeout)
        if result.returncode == 0:
            self._require_shutdown_marker(handle.spec, "execute")
        return result

    def report_failure(
        self,
        spec: EnvironmentSpec,
        *,
        stage: str,
        command_record: Mapping[str, Any],
        attempts: int,
    ) -> dict[str, Any]:
        """Return a redacted, structured failure report with non-silent alternatives."""

        stderr = str(command_record.get("stderr", ""))
        stdout = str(command_record.get("stdout", ""))
        returncode = int(command_record.get("returncode", -1))
        diagnostic_code = (
            "ACCESS_VIOLATION_0xC0000005"
            if returncode in {3221225477, -1073741819}
            else None
        )
        failure_class = (
            "NATIVE_PROCESS_CRASH"
            if diagnostic_code == "ACCESS_VIOLATION_0xC0000005"
            else self._classify_failure(f"{stdout}\n{stderr}")
        )
        alternatives = {
            "DEVICE_GUARD": ["Create a fresh task-local environment", "Ask an administrator to review policy"],
            "VERSION_CONFLICT": ["Use a compatible pinned runtime", "Choose a scientifically equivalent package only after review"],
            "NETWORK": ["Retry with an approved mirror", "Restore from a verified local lock cache"],
            "OFFLINE_CACHE_MISS": [
                "Restore the exact package artifacts into an approved task-local cache",
                "Create a new task-local Conda environment from a verified explicit spec",
            ],
            "COMPILER": ["Use a separate conda binary environment", "Install matching Rtools after separate authorization"],
            "PERMISSION": ["Choose a writable task root", "Request administrator review without changing global policy"],
            "NATIVE_PROCESS_CRASH": [
                "Create a fresh task-local environment and reproduce the exact native exit code",
                "Use a reviewed task-local shutdown workaround only after outputs and completion markers are written",
            ],
            "UNKNOWN": ["Inspect the full task-local install log", "Select a reviewed alternative without silent method substitution"],
        }
        redacted_command = self._redact_command(command_record.get("command", ()))
        return {
            "env_id": spec.env_id,
            "backend": spec.backend,
            "stage": stage,
            "failure_class": failure_class,
            "attempts": min(int(attempts), 2),
            "returncode": returncode,
            "diagnostic_code": diagnostic_code,
            "command": redacted_command,
            "command_fingerprint": self._fingerprint("\0".join(redacted_command)),
            "error_summary": self._redact_error_summary(f"{stdout}\n{stderr}"),
            "stderr_fingerprint": self._fingerprint(stderr),
            "global_changes": False,
            "retry_safe": failure_class in {"NETWORK", "UNKNOWN"} and attempts < 2,
            "alternatives": alternatives.get(failure_class, alternatives["UNKNOWN"]),
            "scientific_method_change_requires_user_choice": True,
        }

    # ---------- command compilation and policy helpers ----------

    def _provision_commands(self, spec: EnvironmentSpec) -> list[list[str]]:
        env_dir = Path(spec.path)
        if spec.backend == "r-renv":
            if not self.rscript.is_absolute():
                raise PolicyError("Rscript must be an absolute path.")
            package_specs = [self._r_package_spec(dep) for dep in spec.dependencies]
            quoted_project = self._r_quote(str(env_dir))
            quoted_specs = ",".join(self._r_quote(item) for item in package_specs)
            declaration_lines = ",".join(
                self._r_quote(f"invisible(requireNamespace('{dep.name}', quietly=TRUE))")
                for dep in spec.dependencies
            )
            expression = (
                f"dir.create({quoted_project}, recursive=TRUE, showWarnings=FALSE);"
                "renv_ns<-asNamespace('renv');pak_ns<-asNamespace('pak');"
                "renv_ns$consent(provided=TRUE);"
                f"renv_ns$init(project={quoted_project},bare=TRUE,load=FALSE,restart=FALSE);"
                f"project_lib<-renv_ns$paths$library(project={quoted_project});"
                "dir.create(project_lib,recursive=TRUE,showWarnings=FALSE);"
            )
            if package_specs:
                if spec.install_strategy == "pins-first":
                    preinstall_names = {name.lower() for name in spec.preinstall}
                    first_specs = [
                        self._r_package_spec(dep)
                        for dep in spec.dependencies
                        if dep.name.lower() in preinstall_names
                    ]
                    remaining_specs = [
                        self._r_package_spec(dep)
                        for dep in spec.dependencies
                        if dep.name.lower() not in preinstall_names
                    ]
                    quoted_first = ",".join(self._r_quote(item) for item in first_specs)
                    expression += (
                        f"pak_ns$pkg_install(c({quoted_first}),lib=project_lib,"
                        "upgrade=FALSE,ask=FALSE);"
                    )
                    if remaining_specs:
                        quoted_remaining = ",".join(
                            self._r_quote(item) for item in remaining_specs
                        )
                        expression += (
                            f"pak_ns$pkg_install(c({quoted_remaining}),lib=project_lib,"
                            "upgrade=FALSE,ask=FALSE);"
                        )
                else:
                    expression += (
                        f"pak_ns$pkg_install(c({quoted_specs}),lib=project_lib,ask=FALSE);"
                    )
                expression += (
                    f"writeLines(c({declaration_lines}),"
                    f"file.path({quoted_project},'environment-requirements.R'),useBytes=TRUE)"
                )
            else:
                expression += "cat(project_lib)"
            expression = expression.rstrip(";") + self._r_shutdown_expression(spec, "provision")
            return [[str(self.rscript), "--vanilla", "-e", expression]]
        if spec.backend == "python-uv":
            uv = shutil.which("uv") or "uv"
            python_exe = env_dir / "Scripts" / "python.exe"
            commands = [[uv, "venv", str(env_dir)]]
            if spec.dependencies:
                commands.append(
                    [uv, "pip", "install", "--python", str(python_exe)]
                    + [self._python_package_spec(dep) for dep in spec.dependencies]
                )
            return commands
        if spec.backend == "conda":
            conda = shutil.which("conda") or "conda"
            runtime_package = "r-base" if spec.runtime == "r" else "python"
            packages = [runtime_package, *[self._conda_package_spec(dep) for dep in spec.dependencies]]
            return [[
                conda,
                "create",
                "--yes",
                "--prefix",
                str(env_dir),
                "--override-channels",
                "--strict-channel-priority",
                "--channel",
                "conda-forge",
                "--channel",
                "bioconda",
                *packages,
            ]]
        raise PolicyError(f"Unsupported backend: {spec.backend}")

    def _verification_command(self, spec: EnvironmentSpec) -> list[str]:
        names = [dep.name for dep in spec.dependencies]
        if spec.runtime == "r":
            if spec.backend == "r-renv":
                quoted_project = self._r_quote(spec.path)
                package_vector = ",".join(self._r_quote(name) for name in names)
                expression = (
                    f"project<-{quoted_project};"
                    "project_lib<-renv::paths$library(project=project);"
                    "stopifnot(dir.exists(project_lib));"
                    "renv::load(project=project,quiet=TRUE);"
                )
                if names:
                    expression += (
                        f"packages<-c({package_vector});"
                        "locations<-vapply(packages,function(pkg) "
                        "find.package(pkg,lib.loc=project_lib,quiet=FALSE),character(1));"
                        "stopifnot(all(nzchar(locations)));"
                    )
                else:
                    expression += "cat(R.version.string)"
                expression += (
                    "if(file.exists(renv::paths$lockfile(project=project))){"
                    "lock<-renv::lockfile_read(renv::paths$lockfile(project=project));"
                    "stopifnot(all(packages %in% names(lock$Packages)));"
                    "installed_versions<-vapply(packages,function(pkg) "
                    "as.character(packageVersion(pkg,lib.loc=project_lib)),character(1));"
                    "locked_versions<-vapply(packages,function(pkg) "
                    "as.character(lock$Packages[[pkg]]$Version),character(1));"
                    "stopifnot(identical(unname(installed_versions),unname(locked_versions)))}"
                )
                expression = expression.rstrip(";") + self._r_shutdown_expression(spec, "verify")
                return [str(self.rscript), "--vanilla", "-e", expression]
            expression = ";".join(f"stopifnot(requireNamespace({self._r_quote(name)},quietly=TRUE))" for name in names)
            expression = expression or "cat(R.version.string)"
            return [self._conda_executable(spec, "Rscript.exe"), "--vanilla", "-e", expression]
        imports = ";".join(f"import {self._python_import_name(name)}" for name in names)
        return [self._python_executable(spec), "-c", imports or "import sys;print(sys.version)"]

    def _freeze_command(self, spec: EnvironmentSpec) -> list[str]:
        if spec.backend == "r-renv":
            quoted_project = self._r_quote(spec.path)
            package_vector = ",".join(
                self._r_quote(name)
                for name in dict.fromkeys([dep.name for dep in spec.dependencies] + ["renv"])
            )
            expression = (
                f"project<-{quoted_project};"
                "project_lib<-renv::paths$library(project=project);"
                f"packages<-c({package_vector});"
                "renv::snapshot(project=project,library=project_lib,packages=packages,"
                "prompt=FALSE,type='all',force=TRUE);"
                "lock<-renv::lockfile_read(renv::paths$lockfile(project=project));"
                "stopifnot(all(packages %in% names(lock$Packages)));"
                "installed_versions<-vapply(packages,function(pkg) "
                "as.character(packageVersion(pkg,lib.loc=project_lib)),character(1));"
                "locked_versions<-vapply(packages,function(pkg) "
                "as.character(lock$Packages[[pkg]]$Version),character(1));"
                "stopifnot(identical(unname(installed_versions),unname(locked_versions)))"
            )
            expression = expression.rstrip(";") + self._r_shutdown_expression(spec, "freeze")
            return [str(self.rscript), "--vanilla", "-e", expression]
        if spec.backend == "python-uv":
            uv = shutil.which("uv") or "uv"
            return [uv, "pip", "freeze", "--python", self._python_executable(spec)]
        if spec.backend == "conda":
            conda = shutil.which("conda") or "conda"
            return [conda, "list", "--explicit", "--prefix", spec.path]
        raise PolicyError(f"Unsupported backend: {spec.backend}")

    def _execution_command(self, spec: EnvironmentSpec, script: Path, args: Sequence[str]) -> list[str]:
        if spec.runtime == "r":
            if spec.backend == "r-renv":
                expression = (
                    f"renv::load(project={self._r_quote(spec.path)},quiet=TRUE);"
                    f"sys.source({self._r_quote(str(script))},envir=globalenv())"
                )
                expression = expression.rstrip(";") + self._r_shutdown_expression(spec, "execute")
                return [
                    str(self.rscript),
                    "--vanilla",
                    "-e",
                    expression,
                    *map(str, args),
                ]
            executable = self._conda_executable(spec, "Rscript.exe")
            return [executable, "--vanilla", str(script), *map(str, args)]
        return [self._python_executable(spec), str(script), *map(str, args)]

    def _backend_lock_path(self, spec: EnvironmentSpec) -> Path:
        names = {
            "r-renv": "renv.lock",
            "python-uv": "requirements.lock.txt",
            "conda": "explicit.txt",
        }
        try:
            name = names[spec.backend]
        except KeyError as exc:
            raise PolicyError(f"Unsupported backend: {spec.backend}") from exc
        return Path(spec.path) / name

    def _cached_backend_lock_matches(
        self,
        spec: EnvironmentSpec,
        marker: Mapping[str, Any],
    ) -> bool:
        evidence = marker.get("backend_lock")
        if not isinstance(evidence, Mapping):
            return False
        relative_path = str(evidence.get("path", ""))
        expected_sha = str(evidence.get("sha256", ""))
        if not relative_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            return False
        env_dir = Path(spec.path).resolve()
        candidate = (env_dir / relative_path).resolve()
        try:
            candidate.relative_to(env_dir)
        except ValueError:
            return False
        if not (
            candidate == self._backend_lock_path(spec).resolve()
            and candidate.is_file()
            and self._sha256_file(candidate) == expected_sha
        ):
            return False
        recorded_workaround = marker.get("windows_exit_workaround", {"enabled": False})
        return recorded_workaround == self._windows_exit_evidence()

    def _windows_exit_evidence(self) -> dict[str, Any]:
        if self.windows_exit_helper is None:
            return {"enabled": False}
        if not self.windows_exit_helper.is_file():
            raise PolicyError(f"Configured Windows R exit helper is missing: {self.windows_exit_helper}")
        return {
            "enabled": True,
            "helper_sha256": self._sha256_file(self.windows_exit_helper),
        }

    def _shutdown_marker(self, spec: EnvironmentSpec, stage: str) -> Path | None:
        if self.windows_exit_helper is None or spec.runtime != "r":
            return None
        return Path(spec.path) / ".manager-markers" / f"{stage}.complete"

    def _r_shutdown_expression(self, spec: EnvironmentSpec, stage: str) -> str:
        marker = self._shutdown_marker(spec, stage)
        if marker is None:
            return ""
        evidence = self._windows_exit_evidence()
        if not evidence["enabled"]:
            return ""
        return (
            f";dir.create({self._r_quote(str(marker.parent))},recursive=TRUE,showWarnings=FALSE);"
            f"writeLines(c('stage={stage}','status=complete'),{self._r_quote(str(marker))},useBytes=TRUE);"
            "flush.console();"
            f"dyn.load({self._r_quote(str(self.windows_exit_helper))});"
            ".Call('biomedical_environment_terminate_process',0L)"
        )

    def _clear_shutdown_marker(self, spec: EnvironmentSpec, stage: str) -> None:
        marker = self._shutdown_marker(spec, stage)
        if marker is not None and marker.is_file():
            marker.unlink()

    def _require_shutdown_marker(self, spec: EnvironmentSpec, stage: str) -> None:
        marker = self._shutdown_marker(spec, stage)
        if marker is not None and not marker.is_file():
            raise ProvisionError(
                f"Windows exit workaround did not write the {stage} completion marker",
                {
                    "env_id": spec.env_id,
                    "backend": spec.backend,
                    "stage": stage,
                    "failure_class": "UNKNOWN",
                    "diagnostic_code": "WINDOWS_EXIT_MARKER_MISSING",
                    "attempts": 1,
                    "global_changes": False,
                    "retry_safe": False,
                    "scientific_method_change_requires_user_choice": True,
                },
            )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _run_with_retry(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        attempt_limit = min(attempts or self.max_attempts, 2)
        result: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, attempt_limit + 1):
            result = self.runner(list(command), cwd=cwd, timeout=3_600)
            if result.returncode == 0:
                break
        assert result is not None
        return {
            "command": list(command),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "attempts": attempt,
        }

    def _validate_plan_roots(self, plan: EnvironmentPlan) -> None:
        if Path(plan.task_root).resolve() != self.task_root or Path(plan.cache_root).resolve() != self.cache_root:
            raise PolicyError("Plan roots do not match this manager.")
        for spec in plan.environments:
            self._assert_managed_path(Path(spec.path))

    def _assert_managed_path(self, path: Path, *, allow_task_root: bool = False) -> None:
        resolved = path.expanduser().resolve()
        try:
            resolved.relative_to(self.task_root)
        except ValueError as exc:
            raise PolicyError(f"Path escapes task root: {resolved}") from exc
        if resolved == self.task_root and not allow_task_root:
            raise PolicyError("The task root itself cannot be used as an environment target.")

    def _validate_dependency(self, dependency: Dependency) -> None:
        allowed = {"cran", "bioconductor", "pypi", "conda-forge", "bioconda", "github"}
        if dependency.source not in allowed:
            raise PolicyError(f"Unsupported dependency source: {dependency.source}")
        if dependency.runtime and dependency.runtime not in {"r", "python"}:
            raise PolicyError(f"Unsupported dependency runtime: {dependency.runtime}")
        if dependency.source in {"cran", "bioconductor"} and dependency.runtime == "python":
            raise PolicyError(f"{dependency.source} dependencies require runtime='r'.")
        if dependency.source == "pypi" and dependency.runtime == "r":
            raise PolicyError("PyPI dependencies require runtime='python'.")
        if not re.match(r"^[A-Za-z0-9_.-]+$", dependency.name):
            raise PolicyError(f"Unsafe dependency name: {dependency.name}")
        if dependency.source == "github":
            if not dependency.repository or not dependency.ref:
                raise PolicyError("GitHub dependencies require repository and ref.")
            repo = self._normalise_repo(dependency.repository)
            if repo not in self.github_allowlist:
                raise PolicyError(f"GitHub repository is not allowlisted: {repo}")
            if not FULL_SHA.fullmatch(dependency.ref):
                raise PolicyError("GitHub dependencies must use a full 40-character commit SHA.")

    @staticmethod
    def _normalise_repo(repository: str) -> str:
        value = repository.strip().rstrip("/")
        value = re.sub(r"^https?://github\.com/", "", value, flags=re.IGNORECASE)
        value = re.sub(r"^git@github\.com:", "", value, flags=re.IGNORECASE)
        return re.sub(r"\.git$", "", value, flags=re.IGNORECASE).lower()

    @staticmethod
    def _dependency_runtime(dependency: Dependency) -> str:
        if dependency.runtime:
            return dependency.runtime
        return "python" if dependency.source == "pypi" else "r" if dependency.source in {"cran", "bioconductor"} else "python"

    @staticmethod
    def _infer_runtimes(raw_dependencies: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
        runtimes: list[str] = []
        for item in raw_dependencies:
            runtime = str(item.get("runtime", "")).lower()
            if not runtime:
                source = str(item.get("source", "cran")).lower()
                runtime = "python" if source == "pypi" else "r"
            if runtime not in runtimes:
                runtimes.append(runtime)
        return tuple(runtimes)

    @staticmethod
    def _select_backend(runtime: str, recipe: Mapping[str, Any], dependencies: Sequence[Dependency]) -> str:
        requested = str(recipe.get("backend", "auto")).lower()
        if requested == "conda" or bool(recipe.get("requires_conda")) or any(
            dependency.source in {"conda-forge", "bioconda"} for dependency in dependencies
        ):
            return "conda"
        if requested not in {"auto", "r-renv", "python-uv"}:
            raise PolicyError(f"Unsupported backend request: {requested}")
        expected = "r-renv" if runtime == "r" else "python-uv"
        if requested != "auto" and requested != expected:
            raise PolicyError(f"Backend {requested} is incompatible with runtime {runtime}.")
        return expected

    def _r_package_spec(self, dependency: Dependency) -> str:
        if dependency.source == "github":
            return f"{self._normalise_repo(dependency.repository or '')}@{dependency.ref}"
        prefix = "bioc::" if dependency.source == "bioconductor" else ""
        suffix = f"@{dependency.version}" if dependency.version else ""
        return f"{prefix}{dependency.name}{suffix}"

    def _python_package_spec(self, dependency: Dependency) -> str:
        if dependency.source == "github":
            repo = self._normalise_repo(dependency.repository or "")
            return f"{dependency.name} @ git+https://github.com/{repo}.git@{dependency.ref}"
        suffix = f"=={dependency.version}" if dependency.version else ""
        return f"{dependency.name}{suffix}"

    @staticmethod
    def _conda_package_spec(dependency: Dependency) -> str:
        suffix = f"={dependency.version}" if dependency.version else ""
        return f"{dependency.conda_name or dependency.name}{suffix}"

    @staticmethod
    def _r_quote(value: str) -> str:
        return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'

    @staticmethod
    def _python_import_name(package_name: str) -> str:
        return package_name.replace("-", "_")

    @staticmethod
    def _platform_key() -> str:
        return f"{platform.system().lower()}-{platform.machine().lower()}"

    @staticmethod
    def _python_executable(spec: EnvironmentSpec) -> str:
        return str(Path(spec.path) / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    @staticmethod
    def _conda_executable(spec: EnvironmentSpec, name: str) -> str:
        return str(Path(spec.path) / (f"Scripts/{name}" if os.name == "nt" else f"bin/{name}"))

    @staticmethod
    def _is_device_guard(text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in DEVICE_GUARD_PATTERNS)

    @classmethod
    def _classify_failure(cls, text: str) -> str:
        lowered = text.lower()
        if cls._is_device_guard(text):
            return "DEVICE_GUARD"
        offline_markers = (
            "network connectivity is disabled",
            "network is disabled",
            "offline mode",
            "--offline",
        )
        cache_miss_markers = (
            "wasn't found in the cache",
            "was not found in the cache",
            "not found in the cache",
            "missing from the cache",
        )
        if any(marker in lowered for marker in offline_markers) and any(
            marker in lowered for marker in cache_miss_markers
        ):
            return "OFFLINE_CACHE_MISS"
        if any(token in lowered for token in ("version conflict", "unsatisfiable", "requires python", "depends on")):
            return "VERSION_CONFLICT"
        if any(
            token in lowered
            for token in (
                "timed out",
                "readtimeout",
                "incompleteread",
                "connectionerror",
                "remote end closed connection",
                "connection",
                "could not resolve",
                "failed to download",
                "failed to fetch",
                "network is unreachable",
                "ssl",
                "proxy",
            )
        ):
            return "NETWORK"
        if any(token in lowered for token in ("compiler", "rtools", "gcc", "make: command not found", "build tools")):
            return "COMPILER"
        if any(token in lowered for token in ("access is denied", "permission denied", "errno 13")):
            return "PERMISSION"
        return "UNKNOWN"

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]

    @staticmethod
    def _redact_command(command: Iterable[Any]) -> list[str]:
        """Return a useful argv outline without retaining credentials or locators.

        Failure reports can be copied into QA reports and public support bundles.  A
        command therefore must not retain an interpreter, cache, project or source
        locator merely because it was passed as an argv element.  The caller records
        a fingerprint of this redacted outline separately for incident correlation.
        """

        return [EnvironmentManager._redact_locator_text(str(part)) for part in command]

    @staticmethod
    def _redact_locator_text(text: str) -> str:
        """Redact credentials plus URL, Windows, UNC and POSIX locators."""

        redacted = re.sub(
            r"(?i)(token|password|secret|api[_-]?key)=([^\s]+)",
            r"\1=<redacted>",
            text,
        )
        redacted = re.sub(r"(?i)(?:file|https?|git\+https?|ssh)://\S+", "<url-redacted>", redacted)
        redacted = re.sub(
            r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\r\n\t;，。]+",
            "<path-redacted>",
            redacted,
        )
        redacted = re.sub(
            r"(^|[\s=\"'(])/(?!/)[^\r\n\t;，。]+",
            r"\1<path-redacted>",
            redacted,
            flags=re.MULTILINE,
        )
        return redacted

    @staticmethod
    def _redact_error_summary(text: str, limit: int = 1200) -> str:
        """Keep actionable package/error text without leaking locators or secrets."""

        redacted = EnvironmentManager._redact_locator_text(text)
        redacted = "\n".join(line.strip() for line in redacted.splitlines() if line.strip())
        return redacted[-limit:]


__all__ = [
    "AuthorizationError",
    "Dependency",
    "EnvironmentHandle",
    "EnvironmentManager",
    "EnvironmentManagerError",
    "EnvironmentPlan",
    "EnvironmentSpec",
    "ExecutionAuthorization",
    "PolicyError",
    "ProvisionError",
]
