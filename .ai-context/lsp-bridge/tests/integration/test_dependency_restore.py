"""
Integration tests for dependency_restore.py — per-language restore-command
selection and lockfile-hash staleness gating. Mocks asyncio.create_subprocess_exec
(no real go/cargo/bundle/pip/dart/swift/dotnet toolchain needed — this is
exactly the "no real network/toolchain invocations" scope from the test
plan) but exercises the REAL gating/state-file logic against a real
filesystem (tmp_path), which is the part actually worth regression-testing.
"""
import asyncio
from pathlib import Path

import pytest

import dependency_restore as dr


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _mock_subprocess(monkeypatch, recorded_calls: list, returncode: int = 0):
    async def fake_create_subprocess_exec(*command, **kwargs):
        recorded_calls.append({"command": command, "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        return _FakeProcess(returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


@pytest.fixture()
def repo_root(tmp_path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def _cache_env(tmp_path, monkeypatch):
    # Every _RESTORE_SPECS entry's cache_root_env must be set for
    # _state_dir() to work — point them all at tmp_path subdirectories.
    for env_var in ("GOMODCACHE", "CARGO_HOME", "NUGET_PACKAGES", "BUNDLE_PATH", "PIP_CACHE_DIR", "PUB_CACHE"):
        monkeypatch.setenv(env_var, str(tmp_path / env_var.lower()))
    monkeypatch.setattr(dr, "_PIP_PROJECT_ROOT", str(tmp_path / "pip_projects"))
    monkeypatch.setattr(dr, "_SWIFTPM_PROJECT_ROOT", str(tmp_path / "swiftpm"))


async def test_no_lockfile_present_skips_restore_without_running_a_command(monkeypatch, repo_root):
    calls: list = []
    _mock_subprocess(monkeypatch, calls)
    result = await dr.restore_dependencies("go", "proj1", str(repo_root))
    assert calls == []
    assert result is None  # go has no scratch dir


async def test_first_run_with_lockfile_runs_the_restore_command(monkeypatch, repo_root):
    (repo_root / "go.sum").write_text("fake-checksum-data\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls)

    await dr.restore_dependencies("go", "proj1", str(repo_root))

    assert len(calls) == 1
    assert calls[0]["command"] == ("go", "mod", "download")
    assert calls[0]["cwd"] == str(repo_root)


async def test_unchanged_lockfile_skips_restore_on_second_run(monkeypatch, repo_root):
    (repo_root / "go.sum").write_text("fake-checksum-data\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls)

    await dr.restore_dependencies("go", "proj1", str(repo_root))
    assert len(calls) == 1  # first run: restore executes

    await dr.restore_dependencies("go", "proj1", str(repo_root))
    assert len(calls) == 1  # second run, same lockfile content: still just 1 — no re-run


async def test_changed_lockfile_triggers_a_fresh_restore(monkeypatch, repo_root):
    lockfile = repo_root / "Cargo.lock"
    lockfile.write_text("version-1\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls)

    await dr.restore_dependencies("rust", "proj1", str(repo_root))
    assert len(calls) == 1

    lockfile.write_text("version-2-different-content\n")
    await dr.restore_dependencies("rust", "proj1", str(repo_root))
    assert len(calls) == 2  # hash changed -> restore runs again
    assert calls[1]["command"] == ("cargo", "fetch")


async def test_python_restore_adds_project_scratch_flag(monkeypatch, tmp_path, repo_root):
    # _RestoreSpec is a frozen dataclass, and _RESTORE_SPECS["python"]'s
    # project_scratch_root was already resolved from the (hardcoded,
    # container-only) _PIP_PROJECT_ROOT constant at module-import time —
    # patching that module attribute afterward doesn't retroactively change
    # the already-built dict entry, so replace the whole entry instead.
    patched_spec = dr._RestoreSpec(
        lockfile="requirements.txt", cache_root_env="PIP_CACHE_DIR",
        command=("pip", "install", "--no-cache-dir", "-r", "requirements.txt"),
        project_scratch_flag="--target", project_scratch_root=str(tmp_path / "pip_projects"),
    )
    monkeypatch.setitem(dr._RESTORE_SPECS, "python", patched_spec)

    (repo_root / "requirements.txt").write_text("httpx==0.28.1\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls)

    scratch_dir = await dr.restore_dependencies("python", "proj1", str(repo_root))

    assert "--target" in calls[0]["command"]
    target_idx = calls[0]["command"].index("--target")
    assert calls[0]["command"][target_idx + 1] == scratch_dir
    assert scratch_dir is not None and scratch_dir.endswith("proj1")


async def test_degraded_language_restore_failure_does_not_raise(monkeypatch, repo_root):
    # Dart is in _DEGRADED_LANGUAGES — a failed restore must be swallowed
    # (logged as an expected degradation), never raised, since dependency
    # navigation degrading must not block the language server from starting
    # at all (see RestoreError's own docstring).
    (repo_root / "pubspec.lock").write_text("fake-lock\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls, returncode=1)

    result = await dr.restore_dependencies("dart", "proj1", str(repo_root))
    assert len(calls) == 1  # it did attempt the restore
    assert result is None  # no exception raised


async def test_non_degraded_language_restore_failure_raises_restore_error(monkeypatch, repo_root):
    (repo_root / "go.sum").write_text("fake-checksum-data\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls, returncode=1)

    with pytest.raises(dr.RestoreError):
        await dr.restore_dependencies("go", "proj1", str(repo_root))


async def test_failed_restore_does_not_update_state_so_next_run_retries(monkeypatch, repo_root):
    (repo_root / "pubspec.lock").write_text("fake-lock\n")
    calls: list = []
    _mock_subprocess(monkeypatch, calls, returncode=1)

    await dr.restore_dependencies("dart", "proj1", str(repo_root))
    assert len(calls) == 1

    # Same lockfile content, but the previous attempt failed and never wrote
    # the state file — this run must retry, not treat it as "up to date".
    await dr.restore_dependencies("dart", "proj1", str(repo_root))
    assert len(calls) == 2


async def test_typescript_and_unknown_languages_are_a_no_op():
    # No spec exists for these — must return None immediately, no subprocess,
    # no _state_dir() call (which would otherwise raise for a missing
    # cache_root_env this language was never given one for).
    assert await dr.restore_dependencies("typescript", "proj1", "/nonexistent") is None
    assert await dr.restore_dependencies("totally-unknown-language", "proj1", "/nonexistent") is None
