"""Tests for the scan engine, driven entirely by fakes — no AiiDA, no Textual."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from aiida_error_inspector import node_inspector as ni
from aiida_error_inspector.classify import Classifier
from aiida_error_inspector.scan import ScanBackend, ScanRequest, run_scan

from .fakes import FakeCalcJob


@dataclass
class Ref:
    """Stand-in for traversal.ProcessRef."""

    pk: int
    exit_status: int | None = None
    depth: int = 1
    label: str = "wc"
    is_calcjob: bool = True


def make_backend(fathers, forest, nodes, *, search=ni.search_file):
    calls = {"load": 0, "forest": 0}

    def load(pks):
        calls["load"] += 1
        return {pk: nodes[pk] for pk in pks if pk in nodes}

    def call_forest(root_pks, *, max_depth=8):
        calls["forest"] += 1
        return {pk: forest.get(pk, []) for pk in root_pks}

    backend = ScanBackend(
        failed_fathers=lambda label: fathers,
        call_forest=call_forest,
        select_candidates=lambda refs, limit=5: [r for r in refs if r.is_calcjob][:limit],
        load_calcjobs=load,
        search_file=search,
    )
    return backend, calls


def out(**files):
    return files


# --------------------------------------------------------------------------- #


def test_tags_a_workchain_whose_calcjob_matches():
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "convergence NOT achieved\n"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    req = ScanRequest(
        group_label="g",
        classifiers=(Classifier(tag="scf", filename="aiida.out", pattern="convergence NOT achieved"),),
    )
    result = run_scan(req, backend=backend)

    assert result.new_tags == {1: {"scf"}}
    assert result.n_fathers == 1
    assert result.n_tagged == 1


def test_one_file_read_serves_every_classifier_on_that_file():
    """The old `u` loop re-read aiida.out once per pattern."""
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "alpha\nbeta\ngamma\n"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    req = ScanRequest(
        group_label="g",
        classifiers=(
            Classifier(tag="a", filename="aiida.out", pattern="alpha"),
            Classifier(tag="b", filename="aiida.out", pattern="beta"),
            Classifier(tag="c", filename="aiida.out", pattern="gamma"),
        ),
    )
    result = run_scan(req, backend=backend)

    assert result.new_tags == {1: {"a", "b", "c"}}
    assert node.output_repo.open_calls == ["aiida.out"]


def test_multi_tag_on_one_node():
    father = Ref(pk=1)
    calc = Ref(pk=10, exit_status=305)
    node = FakeCalcJob(
        pk=10,
        retrieved=out(**{"aiida.out": "MPICH ERROR\n", "_scheduler-stderr.txt": "oom\n"}),
    )
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    req = ScanRequest(
        group_label="g",
        classifiers=(
            Classifier(tag="mpich", filename="aiida.out", pattern="MPICH ERROR"),
            Classifier(tag="oom", filename="_scheduler-stderr.txt", pattern="oom"),
            Classifier(tag="305", kind="exit_code", exit_code=305),
        ),
    )
    result = run_scan(req, backend=backend)
    assert result.new_tags == {1: {"mpich", "oom", "305"}}


def test_exit_code_classifier_does_no_file_io():
    father = Ref(pk=1, exit_status=401)
    calc = Ref(pk=10, exit_status=305)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "x"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    req = ScanRequest(
        group_label="g", classifiers=(Classifier(tag="305", kind="exit_code", exit_code=305),)
    )
    result = run_scan(req, backend=backend)

    assert result.new_tags == {1: {"305"}}
    assert node.output_repo.open_calls == []


def test_exit_code_matches_the_father_itself():
    """An excepted father that never submitted anything is still classifiable."""
    father = Ref(pk=1, exit_status=401)
    backend, _ = make_backend([father], {1: []}, {})

    req = ScanRequest(
        group_label="g", classifiers=(Classifier(tag="401", kind="exit_code", exit_code=401),)
    )
    result = run_scan(req, backend=backend)

    assert result.new_tags == {1: {"401"}}
    assert result.n_no_calcjob == 1


# --------------------------------------------------------------------------- #
# The negative cache
# --------------------------------------------------------------------------- #


def test_already_scanned_nodes_are_skipped_entirely():
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "nothing here\n"}))
    backend, calls = make_backend([father], {1: [calc]}, {10: node})

    classifier = Classifier(tag="a", filename="aiida.out", pattern="zzz")
    req = ScanRequest(group_label="g", classifiers=(classifier,))

    cache = {1: {classifier.fingerprint()}}
    result = run_scan(req, backend=backend, scan_cache=cache)

    assert result.n_skipped_cached == 1
    assert node.output_repo.open_calls == []
    assert calls["load"] == 0


def test_unmatched_nodes_are_recorded_so_they_are_not_re_read():
    """The old categorized.json only recorded matches, so misses were re-read forever."""
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "nothing here\n"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    classifier = Classifier(tag="a", filename="aiida.out", pattern="zzz")
    result = run_scan(ScanRequest(group_label="g", classifiers=(classifier,)), backend=backend)

    assert result.new_tags == {}
    assert result.scanned == {1: {classifier.fingerprint()}}


def test_adding_a_pattern_only_tests_the_new_one():
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "beta\n"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    old = Classifier(tag="a", filename="aiida.out", pattern="alpha")
    new = Classifier(tag="b", filename="aiida.out", pattern="beta")

    cache = {1: {old.fingerprint()}}
    result = run_scan(
        ScanRequest(group_label="g", classifiers=(old, new)), backend=backend, scan_cache=cache
    )

    assert result.new_tags == {1: {"b"}}
    assert result.scanned == {1: {new.fingerprint()}}


def test_force_ignores_the_cache():
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "alpha\n"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    classifier = Classifier(tag="a", filename="aiida.out", pattern="alpha")
    cache = {1: {classifier.fingerprint()}}
    result = run_scan(
        ScanRequest(group_label="g", classifiers=(classifier,), force=True),
        backend=backend,
        scan_cache=cache,
    )
    assert result.new_tags == {1: {"a"}}


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def test_missing_file_is_counted_separately_from_no_match():
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"other.txt": "x"}))
    backend, _ = make_backend([father], {1: [calc]}, {10: node})

    result = run_scan(
        ScanRequest(
            group_label="g",
            classifiers=(Classifier(tag="a", filename="aiida.out", pattern="x"),),
        ),
        backend=backend,
    )
    assert result.missing_file["aiida.out"] == 1
    assert "aiida.out absent in 1" in result.summary()


def test_summary_distinguishes_an_empty_group():
    backend, _ = make_backend([], {}, {})
    result = run_scan(
        ScanRequest(group_label="g", classifiers=(Classifier(tag="a", filename="f", pattern="p"),)),
        backend=backend,
    )
    assert result.summary() == "No failed workchains in this group."


def test_read_errors_are_recorded_not_swallowed():
    father = Ref(pk=1)
    calc = Ref(pk=10)
    node = FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": "x"}))

    def exploding_search(*_a, **_k):
        raise RuntimeError("repository offline")

    backend, _ = make_backend([father], {1: [calc]}, {10: node}, search=exploding_search)
    result = run_scan(
        ScanRequest(
            group_label="g",
            classifiers=(Classifier(tag="a", filename="aiida.out", pattern="x"),),
        ),
        backend=backend,
    )
    assert 1 in result.errors
    assert "read error" in result.summary()


# --------------------------------------------------------------------------- #
# Cancellation and progress
# --------------------------------------------------------------------------- #


def test_cancellation_keeps_partial_results():
    fathers = [Ref(pk=i) for i in range(1, 6)]
    forest = {i: [Ref(pk=100 + i)] for i in range(1, 6)}
    nodes = {100 + i: FakeCalcJob(pk=100 + i, retrieved=out(**{"aiida.out": "alpha\n"})) for i in range(1, 6)}
    backend, _ = make_backend(fathers, forest, nodes)

    seen = {"n": 0}

    def cancel_after_two():
        seen["n"] += 1
        return seen["n"] > 3

    result = run_scan(
        ScanRequest(
            group_label="g",
            classifiers=(Classifier(tag="a", filename="aiida.out", pattern="alpha"),),
        ),
        backend=backend,
        should_cancel=cancel_after_two,
    )
    assert result.cancelled
    assert 0 < len(result.scanned) < 5
    # Only the fathers actually processed are marked scanned, so the rest retry.
    assert set(result.scanned) <= {f.pk for f in fathers}


def test_progress_is_reported():
    fathers = [Ref(pk=i) for i in range(1, 4)]
    forest = {i: [] for i in range(1, 4)}
    backend, _ = make_backend(fathers, forest, {})

    seen = []
    run_scan(
        ScanRequest(
            group_label="g", classifiers=(Classifier(tag="a", kind="exit_code", exit_code=1),)
        ),
        backend=backend,
        progress=seen.append,
    )
    assert seen
    assert seen[-1].total == 3


def test_no_classifiers_is_a_no_op():
    backend, calls = make_backend([Ref(pk=1)], {}, {})
    result = run_scan(ScanRequest(group_label="g", classifiers=()), backend=backend)
    assert result.n_fathers == 0
    assert calls["forest"] == 0


def test_empty_output_tags_the_workchain():
    fathers = [Ref(pk=1), Ref(pk=2), Ref(pk=3)]
    forest = {1: [Ref(pk=10)], 2: [Ref(pk=20)], 3: [Ref(pk=30)]}
    nodes = {
        10: FakeCalcJob(pk=10, retrieved=out(**{"aiida.out": ""})),
        20: FakeCalcJob(pk=20, retrieved=out(**{"aiida.out": "JOB DONE.\n"})),
        30: FakeCalcJob(pk=30, retrieved=out(**{"_scheduler-stderr.txt": "oom\n"})),
    }
    backend, _ = make_backend(fathers, forest, nodes)

    rule = Classifier(tag="empty aiida.out", kind="empty_file", filename="aiida.out")
    result = run_scan(ScanRequest(group_label="g", classifiers=(rule,)), backend=backend)

    assert result.new_tags == {1: {"empty aiida.out"}}
    assert result.missing_file["aiida.out"] == 1
