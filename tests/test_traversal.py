"""Regression tests for the call-graph walk.

These encode the measurement that motivated the rewrite: the old two-level query
could only resolve a father -> WorkChain -> CalcJob topology. A direct
father -> CalcJob and a father -> WC -> WC -> CalcJob both returned nothing,
which is what put roughly half of the user's failed workchains out of reach.
"""

from __future__ import annotations

import pytest

aiida = pytest.importorskip("aiida")

from aiida import orm  # noqa: E402
from aiida.common.links import LinkType  # noqa: E402

from aiida_error_inspector import traversal  # noqa: E402

pytestmark = pytest.mark.db


def _proc(cls, *, state=None, exit_status=None, label="x"):
    node = cls()
    node.base.attributes.set("process_label", label)
    if state is not None:
        node.base.attributes.set("process_state", state)
    if exit_status is not None:
        node.base.attributes.set("exit_status", exit_status)
    return node


def _call(child, parent, link_type):
    child.base.links.add_incoming(parent, link_type=link_type, link_label="call")
    return child


@pytest.fixture
def graph(aiida_profile_clean):
    """Four fathers spanning the topologies that occur in real QE campaigns.

    A: father -> child WC (excepted, no exit_status) -> CalcJob   [depth 2]
    B: father -> CalcJob                                          [depth 1]
    C: father -> WC -> WC -> CalcJob                              [depth 3]
    D: excepted father that never called anything
    """
    made: dict[str, dict] = {}

    fa = _proc(orm.WorkChainNode, state="finished", exit_status=401, label="A").store()
    ca = _call(
        _proc(orm.WorkChainNode, state="excepted", label="A.child"), fa, LinkType.CALL_WORK
    ).store()
    ja = _call(
        _proc(orm.CalcJobNode, state="finished", exit_status=305, label="A.calc"),
        ca,
        LinkType.CALL_CALC,
    ).store()
    made["A"] = {"father": fa.pk, "calcjob": ja.pk, "depth": 2}

    fb = _proc(orm.WorkChainNode, state="finished", exit_status=401, label="B").store()
    jb = _call(
        _proc(orm.CalcJobNode, state="finished", exit_status=305, label="B.calc"),
        fb,
        LinkType.CALL_CALC,
    ).store()
    made["B"] = {"father": fb.pk, "calcjob": jb.pk, "depth": 1}

    fc = _proc(orm.WorkChainNode, state="finished", exit_status=401, label="C").store()
    c1 = _call(
        _proc(orm.WorkChainNode, state="finished", exit_status=401, label="C.1"),
        fc,
        LinkType.CALL_WORK,
    ).store()
    c2 = _call(
        _proc(orm.WorkChainNode, state="finished", exit_status=401, label="C.2"),
        c1,
        LinkType.CALL_WORK,
    ).store()
    jc = _call(
        _proc(orm.CalcJobNode, state="finished", exit_status=305, label="C.calc"),
        c2,
        LinkType.CALL_CALC,
    ).store()
    made["C"] = {"father": fc.pk, "calcjob": jc.pk, "depth": 3}

    fd = _proc(orm.WorkChainNode, state="excepted", label="D").store()
    made["D"] = {"father": fd.pk, "calcjob": None, "depth": None}

    for node in (ja, ca, fa, jb, fb, jc, c2, c1, fc, fd):
        node.seal()
    return made


def _old_query(father_pk):
    """The superseded traversal, kept to prove the regression is real."""
    qb = orm.QueryBuilder()
    qb.append(orm.WorkChainNode, filters={"id": father_pk}, tag="father")
    qb.append(
        orm.WorkChainNode,
        with_incoming="father",
        filters={"attributes.exit_status": {"!==": 0}},
        tag="child_wc",
    )
    qb.append(orm.CalcJobNode, with_incoming="child_wc", project=["id"], tag="calcjob")
    qb.order_by({"calcjob": {"ctime": "desc"}})
    return [row[0] for row in qb.limit(1).all()]


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_call_forest_finds_the_calcjob_at_every_depth(graph, case):
    father = graph[case]["father"]
    forest = traversal.call_forest([father])
    found = traversal.select_candidate_calcjobs(forest[father])
    assert [ref.pk for ref in found] == [graph[case]["calcjob"]]
    assert found[0].depth == graph[case]["depth"]


@pytest.mark.parametrize("case", ["B", "C"])
def test_old_query_missed_these(graph, case):
    """Depth 1 and depth 3 are exactly what the old query could not see."""
    assert _old_query(graph[case]["father"]) == []


def test_old_query_handled_depth_two(graph):
    assert _old_query(graph["A"]["father"]) == [graph["A"]["calcjob"]]


def test_with_ancestors_finds_nothing_over_call_links(graph):
    """Guard against ever 'simplifying' the BFS into a recursive join.

    QueryBuilder's recursive walk follows CREATE/INPUT_CALC only, so this
    returns nothing at any depth.
    """
    for case in ("A", "B", "C"):
        qb = orm.QueryBuilder()
        qb.append(orm.WorkChainNode, filters={"id": graph[case]["father"]}, tag="f")
        qb.append(orm.CalcJobNode, with_ancestors="f", project=["id"])
        assert qb.all() == [], f"with_ancestors unexpectedly resolved case {case}"


def test_father_with_no_called_process(graph):
    father = graph["D"]["father"]
    forest = traversal.call_forest([father])
    assert forest[father] == []
    assert traversal.select_candidate_calcjobs(forest[father]) == []


def test_call_forest_batches_all_roots_together(graph):
    roots = [graph[c]["father"] for c in ("A", "B", "C", "D")]
    forest = traversal.call_forest(roots)
    assert set(forest) == set(roots)
    for case in ("A", "B", "C"):
        pks = {ref.pk for ref in forest[graph[case]["father"]]}
        assert graph[case]["calcjob"] in pks


def test_failed_filter_includes_excepted_fathers(graph):
    """The old father filter required process_state == 'finished'."""
    old = (
        orm.QueryBuilder()
        .append(
            orm.WorkChainNode,
            filters={
                "and": [
                    {"attributes.exit_status": {"!==": 0}},
                    {"attributes.process_state": "finished"},
                ]
            },
            project=["attributes.process_label"],
        )
        .all(flat=True)
    )
    new = (
        orm.QueryBuilder()
        .append(
            orm.WorkChainNode,
            filters=traversal.FAILED_PROCESS_FILTER,
            project=["attributes.process_label"],
        )
        .all(flat=True)
    )
    assert "D" not in old
    assert "D" in new


def test_not_equals_zero_matches_an_absent_exit_status(graph):
    """Documents surprising AiiDA behaviour the new filter deliberately avoids.

    ``{"!==": 0}`` compiles to ``NOT CASE WHEN ... ELSE false END``, so a node
    with no ``exit_status`` attribute yields ``NOT FALSE`` = TRUE and matches.
    """
    excepted = (
        orm.QueryBuilder()
        .append(
            orm.ProcessNode,
            filters={"attributes.process_state": "excepted"},
            project=["id"],
        )
        .all(flat=True)
    )
    matched = (
        orm.QueryBuilder()
        .append(
            orm.ProcessNode,
            filters={
                "and": [
                    {"id": {"in": excepted}},
                    {"attributes.exit_status": {"!==": 0}},
                ]
            },
            project=["id"],
        )
        .all(flat=True)
    )
    assert set(matched) == set(excepted)


def test_select_candidate_calcjobs_prefers_deepest_then_newest(graph):
    father = graph["C"]["father"]
    forest = traversal.call_forest([father])
    refs = forest[father]
    ordered = traversal.select_candidate_calcjobs(refs, limit=None, only_failed=False)
    depths = [r.depth for r in ordered]
    assert depths == sorted(depths, reverse=True)
