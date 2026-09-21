"""End-to-end scan against a real (temporary) AiiDA profile.

Builds a group that mirrors the topologies in a real QE campaign — including
the ones the old traversal could not reach — then runs the real scan engine
over real repository files.
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("aiida")

from aiida import orm  # noqa: E402
from aiida.common.links import LinkType  # noqa: E402

from aiida_error_inspector import storage, traversal  # noqa: E402
from aiida_error_inspector.classify import Classifier  # noqa: E402
from aiida_error_inspector.scan import ScanRequest, run_scan  # noqa: E402

pytestmark = pytest.mark.db

SCF_FAIL = b"""
     iteration #  1     ecut=    60.00 Ry     beta= 0.70
     total energy              =    -123.45678901 Ry

     convergence NOT achieved after 100 iterations: stopping

     Error in routine electrons (1):
"""

MPICH_FAIL = b"MPICH ERROR [Rank 3] Out of memory\n"

CLEAN = b"     convergence has been achieved in  12 iterations\n     JOB DONE.\n"


def _wc(label, state="finished", exit_status=None):
    node = orm.WorkChainNode()
    node.base.attributes.set("process_label", label)
    node.base.attributes.set("process_state", state)
    if exit_status is not None:
        node.base.attributes.set("exit_status", exit_status)
    return node


def _calc(label, state="finished", exit_status=None, files=None):
    node = orm.CalcJobNode()
    node.base.attributes.set("process_label", label)
    node.base.attributes.set("process_state", state)
    if exit_status is not None:
        node.base.attributes.set("exit_status", exit_status)
    node._files = files or {}
    return node


def _attach_retrieved(calc, files):
    retrieved = orm.FolderData()
    for name, content in files.items():
        retrieved.base.repository.put_object_from_filelike(io.BytesIO(content), name)
    retrieved.base.links.add_incoming(calc, link_type=LinkType.CREATE, link_label="retrieved")
    retrieved.store()
    return retrieved


@pytest.fixture
def campaign(aiida_profile_clean):
    """A group of four failed workchains covering four real topologies."""
    group = orm.Group(label="electrides-test").store()
    made = {}

    # 1. depth 2, SCF failure — the only shape the old query could resolve.
    f1 = _wc("PwRelaxWorkChain", "finished", 401).store()
    c1 = _wc("PwBaseWorkChain", "finished", 410)
    c1.base.links.add_incoming(f1, link_type=LinkType.CALL_WORK, link_label="base")
    c1.store()
    j1 = _calc("PwCalculation", "finished", 305)
    j1.base.links.add_incoming(c1, link_type=LinkType.CALL_CALC, link_label="calc")
    j1.store()
    _attach_retrieved(j1, {"aiida.out": SCF_FAIL})
    made["scf_depth2"] = f1.pk

    # 2. depth 1 — father calls the CalcJob directly. Previously unreachable.
    f2 = _wc("PwBaseWorkChain", "finished", 410).store()
    j2 = _calc("PwCalculation", "finished", 305)
    j2.base.links.add_incoming(f2, link_type=LinkType.CALL_CALC, link_label="calc")
    j2.store()
    _attach_retrieved(j2, {"_scheduler-stderr.txt": MPICH_FAIL})
    made["mpich_depth1"] = f2.pk

    # 3. depth 3 — previously unreachable.
    f3 = _wc("EosWorkChain", "finished", 401).store()
    m3 = _wc("PwRelaxWorkChain", "finished", 401)
    m3.base.links.add_incoming(f3, link_type=LinkType.CALL_WORK, link_label="relax")
    m3.store()
    c3 = _wc("PwBaseWorkChain", "finished", 410)
    c3.base.links.add_incoming(m3, link_type=LinkType.CALL_WORK, link_label="base")
    c3.store()
    j3 = _calc("PwCalculation", "finished", 305)
    j3.base.links.add_incoming(c3, link_type=LinkType.CALL_CALC, link_label="calc")
    j3.store()
    _attach_retrieved(j3, {"aiida.out": SCF_FAIL, "CRASH": b"Error in routine davcio\n"})
    made["scf_depth3"] = f3.pk

    # 4. excepted father that never submitted anything. The old father filter
    #    required process_state == "finished", so it was never even a candidate.
    f4 = _wc("PwRelaxWorkChain", "excepted").store()
    made["excepted"] = f4.pk

    # 5. a workchain that succeeded — must never be tagged.
    f5 = _wc("PwRelaxWorkChain", "finished", 0).store()
    j5 = _calc("PwCalculation", "finished", 0)
    j5.base.links.add_incoming(f5, link_type=LinkType.CALL_CALC, link_label="calc")
    j5.store()
    _attach_retrieved(j5, {"aiida.out": CLEAN})
    made["clean"] = f5.pk

    for node in (j1, c1, f1, j2, f2, j3, c3, m3, f3, f4, j5, f5):
        node.seal()
    group.add_nodes([f1, f2, f3, f4, f5])
    return made


# --------------------------------------------------------------------------- #


def test_failed_fathers_includes_excepted_and_excludes_success(campaign):
    fathers = {f.pk for f in traversal.failed_workchains_in_group("electrides-test")}
    assert campaign["excepted"] in fathers
    assert campaign["clean"] not in fathers
    assert len(fathers) == 4


def test_scan_tags_every_topology(campaign):
    classifiers = (
        Classifier(tag="scf", filename="aiida.out", pattern="convergence NOT achieved"),
        Classifier(tag="mpich", filename="_scheduler-stderr.txt", pattern="MPICH ERROR"),
    )
    result = run_scan(ScanRequest(group_label="electrides-test", classifiers=classifiers))

    assert result.new_tags.get(campaign["scf_depth2"]) == {"scf"}
    assert result.new_tags.get(campaign["mpich_depth1"]) == {"mpich"}, "depth-1 topology"
    assert result.new_tags.get(campaign["scf_depth3"]) == {"scf"}, "depth-3 topology"
    assert campaign["clean"] not in result.new_tags


def test_old_traversal_would_have_missed_two_of_them(campaign):
    """Quantifies the regression this rewrite fixes."""

    def old(father_pk):
        qb = orm.QueryBuilder()
        qb.append(orm.WorkChainNode, filters={"id": father_pk}, tag="father")
        qb.append(
            orm.WorkChainNode,
            with_incoming="father",
            filters={"attributes.exit_status": {"!==": 0}},
            tag="child",
        )
        qb.append(orm.CalcJobNode, with_incoming="child", project=["id"])
        return qb.limit(1).all()

    assert old(campaign["scf_depth2"]) != []
    assert old(campaign["mpich_depth1"]) == []
    assert old(campaign["scf_depth3"]) == []


def test_exit_code_classifier_needs_no_files(campaign):
    result = run_scan(
        ScanRequest(
            group_label="electrides-test",
            classifiers=(Classifier(tag="305", kind="exit_code", exit_code=305),),
        )
    )
    tagged = {pk for pk, tags in result.new_tags.items() if "305" in tags}
    assert tagged == {
        campaign["scf_depth2"],
        campaign["mpich_depth1"],
        campaign["scf_depth3"],
    }


def test_excepted_father_is_classifiable_by_state(campaign):
    """The single biggest recovery: fathers that died before submitting."""
    result = run_scan(
        ScanRequest(
            group_label="electrides-test",
            classifiers=(Classifier(tag="401", kind="exit_code", exit_code=401),),
        )
    )
    # The excepted father has no exit_status and no CalcJob, but it *is* now a
    # scan candidate, which it never was before.
    assert campaign["excepted"] in result.scanned
    assert result.n_no_calcjob >= 1


def test_crash_file_is_reachable(campaign):
    result = run_scan(
        ScanRequest(
            group_label="electrides-test",
            classifiers=(
                Classifier(tag="davcio", filename="CRASH", pattern="Error in routine davcio"),
            ),
        )
    )
    assert result.new_tags.get(campaign["scf_depth3"]) == {"davcio"}


def test_rescan_with_cache_does_no_work(campaign):
    classifiers = (
        Classifier(tag="scf", filename="aiida.out", pattern="convergence NOT achieved"),
    )
    first = run_scan(ScanRequest(group_label="electrides-test", classifiers=classifiers))

    cache = {pk: set(fps) for pk, fps in first.scanned.items()}
    second = run_scan(
        ScanRequest(group_label="electrides-test", classifiers=classifiers), scan_cache=cache
    )
    assert second.n_skipped_cached == second.n_fathers
    assert second.new_tags == {}


def test_tags_survive_a_save_load_round_trip(campaign, tmp_path):
    classifiers = (
        Classifier(tag="scf", filename="aiida.out", pattern="convergence NOT achieved"),
        Classifier(tag="305", kind="exit_code", exit_code=305),
    )
    result = run_scan(ScanRequest(group_label="electrides-test", classifiers=classifiers))

    path = tmp_path / "tags.json"
    storage.atomic_write_json(path, storage.serialise_tags(result.new_tags))
    reloaded, _ = storage.load_json(path, {})
    assert storage.parse_tags(reloaded) == {
        pk: tags for pk, tags in result.new_tags.items() if tags
    }
    # A node carrying both a pattern tag and an exit-code tag round-trips.
    assert any(len(tags) > 1 for tags in result.new_tags.values())
