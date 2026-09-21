"""Test configuration.

DB-backed tests use a throwaway ``core.sqlite_dos`` profile so no PostgreSQL
cluster (and no ``pgtest``) is required. They are marked ``db`` and skipped
automatically when aiida-core is not importable.
"""

import pytest

try:  # pragma: no cover - import guard
    import aiida  # noqa: F401

    HAS_AIIDA = True
except ImportError:  # pragma: no cover
    HAS_AIIDA = False

if HAS_AIIDA:
    pytest_plugins = ["aiida.manage.tests.pytest_fixtures"]


def pytest_collection_modifyitems(config, items):
    if HAS_AIIDA:
        return
    skip = pytest.mark.skip(reason="aiida-core not installed")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


if HAS_AIIDA:

    @pytest.fixture(scope="session", autouse=True)
    def aiida_profile(aiida_manager, aiida_profile_factory, tmp_path_factory):
        """Session profile backed by sqlite_dos instead of the psql_dos default."""
        yield aiida_profile_factory(
            {
                "storage": {
                    "backend": "core.sqlite_dos",
                    "config": {"filepath": str(tmp_path_factory.mktemp("sqlite_dos"))},
                }
            }
        )
