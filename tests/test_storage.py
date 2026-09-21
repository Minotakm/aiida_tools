"""Tests for persistence: data-dir resolution, atomic writes, tag round-trips.

None of this needs an AiiDA profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiida_error_inspector import storage


# --------------------------------------------------------------------------- #
# Data directory
# --------------------------------------------------------------------------- #


def test_explicit_data_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(storage.ENV_DATA_DIR, str(tmp_path / "from_env"))
    target = tmp_path / "explicit"
    assert storage.resolve_data_dir(target) == target
    assert target.is_dir()


def test_env_var_used_when_no_explicit_dir(tmp_path, monkeypatch):
    target = tmp_path / "from_env"
    monkeypatch.setenv(storage.ENV_DATA_DIR, str(target))
    assert storage.resolve_data_dir() == target
    assert target.is_dir()


# --------------------------------------------------------------------------- #
# Atomic writes
# --------------------------------------------------------------------------- #


def test_atomic_write_round_trip(tmp_path):
    path = tmp_path / "x.json"
    storage.atomic_write_json(path, {"a": [1, 2]})
    assert json.loads(path.read_text()) == {"a": [1, 2]}


def test_atomic_write_leaves_original_intact_on_failure(tmp_path):
    path = tmp_path / "x.json"
    storage.atomic_write_json(path, {"good": 1})

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        storage.atomic_write_json(path, {"bad": Unserialisable()})

    # The pre-existing file must survive a failed write untouched.
    assert json.loads(path.read_text()) == {"good": 1}
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".x.json")]
    assert leftovers == []


def test_load_json_quarantines_corrupt_file(tmp_path):
    path = tmp_path / "tags.json"
    path.write_text('{"truncated": [1, 2')

    value, quarantined = storage.load_json(path, default={})

    assert value == {}
    assert quarantined is not None
    assert quarantined.exists()
    assert not path.exists()
    assert quarantined.read_text() == '{"truncated": [1, 2'


def test_load_json_missing_file_returns_default(tmp_path):
    value, quarantined = storage.load_json(tmp_path / "nope.json", default={"d": 1})
    assert value == {"d": 1}
    assert quarantined is None


# --------------------------------------------------------------------------- #
# Tag parsing / serialisation
# --------------------------------------------------------------------------- #


def test_parse_current_format():
    data = {"scf": [3, 1, 2], "mpich": [2]}
    assert storage.parse_tags(data) == {1: {"scf"}, 2: {"scf", "mpich"}, 3: {"scf"}}


def test_parse_legacy_pk_to_tag_format():
    assert storage.parse_tags({"10": "scf", "11": "bands"}) == {
        10: {"scf"},
        11: {"bands"},
    }


def test_parse_versioned_envelope():
    data = {"version": 2, "tags": {"scf": [1]}}
    assert storage.parse_tags(data) == {1: {"scf"}}


def test_numeric_tag_name_is_not_mistaken_for_legacy_format():
    """A tag named "305" is exactly what exit-code tagging produces.

    The old heuristic (``next(iter(data)).isdigit()``) inspected only the first
    key and would decode this whole file as the legacy {pk: tag} layout,
    yielding list-valued "tags" that break rendering downstream.
    """
    data = {"305": [111, 222], "scf": [333]}
    assert storage.parse_tags(data) == {
        111: {"305"},
        222: {"305"},
        333: {"scf"},
    }


def test_round_trip_preserves_multi_tag():
    tags = {1: {"scf", "mpich"}, 2: {"bands"}}
    assert storage.parse_tags(storage.serialise_tags(tags)) == tags


def test_round_trip_is_stable_and_sorted():
    tags = {5: {"b"}, 1: {"a"}, 3: {"a"}}
    payload = storage.serialise_tags(tags)
    assert payload["version"] == storage.TAGS_FORMAT_VERSION
    assert payload["tags"] == {"a": [1, 3], "b": [5]}
    assert storage.serialise_tags(storage.parse_tags(payload)) == payload


def test_parse_empty_and_garbage():
    assert storage.parse_tags({}) == {}
    assert storage.parse_tags(None) == {}
    assert storage.parse_tags({"a": 5}) == {}


def test_tag_counts():
    tags = {1: {"scf", "mpich"}, 2: {"scf"}}
    assert storage.tag_counts(tags) == {"scf": 2, "mpich": 1}


def test_real_repo_tags_file_round_trips():
    """Guard the user's actual data: 72 PKs across 3 tags must survive."""
    repo_tags = (
        Path(__file__).resolve().parent.parent / "data" / "tags.json"
    )
    if not repo_tags.exists():
        pytest.skip("repository data/tags.json not present")

    original = json.loads(repo_tags.read_text())
    parsed = storage.parse_tags(original)

    expected_pairs = {
        (int(pk), tag) for tag, pks in original.items() for pk in pks
    }
    actual_pairs = {(pk, tag) for pk, tags in parsed.items() for tag in tags}
    assert actual_pairs == expected_pairs

    # And re-encoding loses nothing.
    assert storage.parse_tags(storage.serialise_tags(parsed)) == parsed


# --------------------------------------------------------------------------- #
# Scan cache
# --------------------------------------------------------------------------- #


def test_scan_cache_round_trip():
    cache = {1: {"fp-a", "fp-b"}, 2: set()}
    assert storage.parse_scan_cache(storage.serialise_scan_cache(cache)) == cache


def test_scan_cache_migrates_from_categorized_list():
    assert storage.parse_scan_cache([7, 8]) == {7: set(), 8: set()}


def test_merge_scan_cache():
    cache = {1: {"old"}}
    storage.merge_scan_cache(cache, [1, 2], ["new"])
    assert cache == {1: {"old", "new"}, 2: {"new"}}
