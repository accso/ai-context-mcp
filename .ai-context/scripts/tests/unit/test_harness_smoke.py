"""
Smoke test proving the scripts/ Tier 1 (unit) harness works. validate_config.py
is pure-stdlib (no pyyaml/psycopg2 etc. needed for its own logic — only its
_pg_* methods touch a live Postgres, and those are out of scope for Tier 1).
"""
import validate_config  # noqa: F401  (proves sys.path wiring resolves the module)


def test_harness_smoke_unit_tier_is_wired_up():
    assert 1 + 1 == 2


def test_validate_config_module_is_importable():
    assert hasattr(validate_config, "_detect_profile")
    assert hasattr(validate_config, "Validator")
