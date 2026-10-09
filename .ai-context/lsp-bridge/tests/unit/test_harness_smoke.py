"""
Smoke test proving the lsp-bridge Tier 1 (unit) harness works. Imports
dependency_restore.py specifically because it's pure-stdlib (asyncio,
hashlib, subprocess, dataclasses) — unlike main.py, it needs no
fastapi/multilspy install to exercise its restore-command-selection and
staleness-hash logic in a fast unit test (see docs/lsp-integration.md).
"""
import dependency_restore  # noqa: F401  (proves sys.path wiring resolves the module)


def test_harness_smoke_unit_tier_is_wired_up():
    assert 1 + 1 == 2


def test_dependency_restore_module_is_importable():
    assert hasattr(dependency_restore, "_hash_file")
