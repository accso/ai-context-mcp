"""
Smoke test proving the watcher Tier 1 (unit) harness works: pytest discovers
this file, conftest.py's sys.path wiring lets bare `import chunker` succeed,
and pytest.ini's testpaths pick up watcher/tests. Real unit-test suites
(chunker.py chunk boundaries, symbol_extractor.py, import_extractor.py) are
added as their own separate change — this file only proves the harness.
"""
import chunker  # noqa: F401  (proves sys.path wiring resolves the module)


def test_harness_smoke_unit_tier_is_wired_up():
    assert 1 + 1 == 2


def test_chunker_module_is_importable():
    # chunk_file is the module's main entry point (see conftest.py docstring).
    assert hasattr(chunker, "chunk_file")
