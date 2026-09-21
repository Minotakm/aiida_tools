"""Tests for the classification rules. No AiiDA profile needed."""

from __future__ import annotations

import json

import pytest

from aiida_error_inspector import classify
from aiida_error_inspector.classify import Classifier, ClassifierError


# --------------------------------------------------------------------------- #
# Construction / validation
# --------------------------------------------------------------------------- #


def test_substring_is_the_default_kind():
    c = Classifier(tag="scf", filename="aiida.out", pattern="convergence NOT achieved")
    assert c.kind == classify.KIND_SUBSTRING
    assert c.needs_file() == "aiida.out"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tag": "", "filename": "a", "pattern": "b"},
        {"tag": "t", "kind": "nonsense"},
        {"tag": "t", "pattern": "b"},  # no filename
        {"tag": "t", "filename": "a"},  # no pattern
        {"tag": "t", "kind": "exit_code"},  # no code
        {"tag": "t", "kind": "regex", "filename": "a", "pattern": "["},  # bad regex
    ],
)
def test_invalid_definitions_are_rejected(kwargs):
    with pytest.raises(ClassifierError):
        Classifier(**kwargs)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def test_substring_is_case_insensitive_by_default():
    c = Classifier(tag="t", filename="aiida.out", pattern="MPICH ERROR")
    assert c.matches_text("fatal: mpich error on rank 3")


def test_substring_can_be_case_sensitive():
    c = Classifier(tag="t", filename="f", pattern="CRASH", case_sensitive=True)
    assert c.matches_text("CRASH detected")
    assert not c.matches_text("crash detected")


def test_regex_matching():
    c = Classifier(
        tag="t", kind="regex", filename="aiida.out", pattern=r"Error in routine\s+(\w+)"
    )
    assert c.matches_text("     Error in routine  electrons (1):")
    assert not c.matches_text("all good")


def test_regex_honours_case_sensitivity():
    ci = Classifier(tag="t", kind="regex", filename="f", pattern="^job done")
    cs = Classifier(tag="t", kind="regex", filename="f", pattern="^job done", case_sensitive=True)
    assert ci.matches_text("JOB DONE")
    assert not cs.matches_text("JOB DONE")


def test_exit_code_matching():
    c = Classifier(tag="305", kind="exit_code", exit_code=305)
    assert c.matches_exit(305)
    assert not c.matches_exit(0)
    assert not c.matches_exit(None)
    assert c.needs_file() is None


def test_kinds_do_not_cross_match():
    text = Classifier(tag="t", filename="f", pattern="x")
    code = Classifier(tag="t", kind="exit_code", exit_code=1)
    assert not text.matches_exit(1)
    assert not code.matches_text("x")


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #


def test_fingerprint_is_stable_across_instances():
    a = Classifier(tag="t", filename="f", pattern="p")
    b = Classifier(tag="t", filename="f", pattern="p")
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_changes_with_the_question_asked():
    base = Classifier(tag="t", filename="f", pattern="p")
    assert base.fingerprint() != Classifier(tag="t", filename="f", pattern="q").fingerprint()
    assert base.fingerprint() != Classifier(tag="t", filename="g", pattern="p").fingerprint()
    assert (
        base.fingerprint()
        != Classifier(tag="t", filename="f", pattern="p", case_sensitive=True).fingerprint()
    )


# --------------------------------------------------------------------------- #
# Serialisation / back-compat
# --------------------------------------------------------------------------- #


def test_legacy_entry_without_kind_loads_as_substring():
    c = Classifier.from_json(
        "SCF convergence issue",
        {"filename": "aiida.out", "pattern": "convergence NOT achieved"},
    )
    assert c.kind == classify.KIND_SUBSTRING
    assert c.case_sensitive is False
    assert c.matches_text("     convergence NOT achieved after 100 iterations")


def test_real_repo_patterns_file_loads():
    """The three patterns already on disk must keep working untouched."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "data" / "patterns.json"
    if not path.exists():
        pytest.skip("repository data/patterns.json not present")

    classifiers, errors = classify.load_classifiers(json.loads(path.read_text()))
    assert errors == []
    assert {c.tag for c in classifiers} == {
        "SCF convergence issue",
        "Wrong bands read from file",
        "MPICH error (might be diagonalisation issue)",
    }
    assert all(c.kind == classify.KIND_SUBSTRING for c in classifiers)


def test_round_trip():
    original = [
        Classifier(tag="a", filename="aiida.out", pattern="p"),
        Classifier(tag="b", kind="regex", filename="f", pattern=r"\d+", case_sensitive=True),
        Classifier(tag="305", kind="exit_code", exit_code=305),
    ]
    reloaded, errors = classify.load_classifiers(classify.dump_classifiers(original))
    assert errors == []
    assert {c.fingerprint() for c in reloaded} == {c.fingerprint() for c in original}


def test_one_bad_entry_does_not_stop_the_rest():
    data = {"good": {"filename": "f", "pattern": "p"}, "bad": {"kind": "exit_code"}}
    classifiers, errors = classify.load_classifiers(data)
    assert [c.tag for c in classifiers] == ["good"]
    assert len(errors) == 1


def test_defaults_are_not_written_out():
    blob = Classifier(tag="t", filename="f", pattern="p").to_json()
    assert "case_sensitive" not in blob
    assert blob == {"kind": "substring", "filename": "f", "pattern": "p"}


# --------------------------------------------------------------------------- #
# Grouping — the property that makes the single-pass scan possible
# --------------------------------------------------------------------------- #


def test_group_by_file_buckets_one_read_per_file():
    classifiers = [
        Classifier(tag="a", filename="aiida.out", pattern="x"),
        Classifier(tag="b", filename="aiida.out", pattern="y"),
        Classifier(tag="c", filename="_scheduler-stderr.txt", pattern="z"),
        Classifier(tag="d", kind="exit_code", exit_code=305),
    ]
    buckets = classify.group_by_file(classifiers)
    assert set(buckets) == {"aiida.out", "_scheduler-stderr.txt", None}
    assert len(buckets["aiida.out"]) == 2
    assert len(buckets[None]) == 1


def test_describe():
    assert Classifier(tag="t", kind="exit_code", exit_code=305).describe() == "exit_status == 305"
    assert "contains" in Classifier(tag="t", filename="f", pattern="p").describe()
