"""End-to-end navigation tests driving the real Textual app with fake queries.

The headline case is ``test_drilling_into_a_workchain_while_tag_filtered``:
press ``T`` to show tagged rows only, then ``a`` to explore one. That used to
show an empty table, which is the bug reported in things_to_implement.txt.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("textual")

from aiida_error_inspector import app as app_module  # noqa: E402


# --------------------------------------------------------------------------- #
# A fake ORM: real enough for isinstance checks and attribute access.
# --------------------------------------------------------------------------- #


class FakeProcessNode:
    def __init__(self, pk, label="Proc", state="finished", exit_status=None):
        self.pk = pk
        self.uuid = f"{pk:08d}-aaaa-bbbb-cccc-dddddddddddd"
        self.process_label = label
        self.process_state = state
        self.exit_status = exit_status
        self.node_type = "process.workflow.workchain.WorkChainNode."
        self.ctime = datetime(2025, 1, 1)
        self.mtime = datetime(2025, 1, 2)


class FakeWorkChainNode(FakeProcessNode):
    """Mirrors AiiDA: both concrete kinds derive from ProcessNode."""


class FakeCalcJobNode(FakeProcessNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.node_type = "process.calculation.calcjob.CalcJobNode."


class FakeGroup:
    def __init__(self, label):
        self.label = label
        self.pk = 1


# Topology used by the tests:
#   WC 100 (tagged 'scf')  -> WC 200 -> CalcJob 300
#   WC 101 (untagged)      -> CalcJob 301
NODES = {
    100: FakeWorkChainNode(100, "PwRelaxWorkChain", "finished", 401),
    101: FakeWorkChainNode(101, "PwRelaxWorkChain", "excepted"),
    200: FakeWorkChainNode(200, "PwBaseWorkChain", "finished", 401),
    300: FakeCalcJobNode(300, "PwCalculation", "finished", 305),
    301: FakeCalcJobNode(301, "PwCalculation", "finished", 305),
}

def _row(node):
    """get_descendants now returns projected dicts, not ORM instances."""
    return {
        "pk": node.pk,
        "node_type": node.node_type,
        "process_label": node.process_label,
        "process_state": node.process_state,
        "exit_status": node.exit_status,
    }


DESCENDANTS = {
    100: [_row(NODES[200])],
    200: [_row(NODES[300])],
    101: [_row(NODES[301])],
    300: [],
    301: [],
}


class FakeOrm:
    WorkChainNode = FakeWorkChainNode
    CalcJobNode = FakeCalcJobNode
    ProcessNode = FakeProcessNode

    @staticmethod
    def load_node(pk):
        return NODES[pk]


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "orm", FakeOrm)
    monkeypatch.setattr(app_module, "load_group", lambda label: FakeGroup(str(label)))
    monkeypatch.setattr(
        app_module,
        "get_groups",
        lambda: [{"label": "electrides", "type_string": "core", "n_nodes": 2}],
    )
    monkeypatch.setattr(
        app_module,
        "get_nodes_in_group",
        lambda label: [
            (100, NODES[100].uuid, NODES[100].node_type, None, "PwRelaxWorkChain", "finished", 401),
            (101, NODES[101].uuid, NODES[101].node_type, None, "PwRelaxWorkChain", "excepted", None),
        ],
    )
    monkeypatch.setattr(app_module, "get_descendants", lambda node: DESCENDANTS[node.pk])
    monkeypatch.setattr(
        app_module.node_inspector,
        "list_all_files",
        lambda node: [("aiida.out", "output"), ("CRASH", "output")],
    )
    monkeypatch.setattr(
        app_module.node_inspector, "file_size", lambda *a, **k: (1024, False)
    )
    monkeypatch.setattr(
        app_module.node_inspector,
        "read_preview",
        lambda *a, **k: app_module.node_inspector.Preview([], ["output line"], 1, 0),
    )

    instance = app_module.GroupNodesApp(data_dir=tmp_path)
    instance.tags = {100: {"scf"}}
    return instance


async def _enter_group(pilot):
    await pilot.press("a")
    await pilot.pause()


async def _select_pk(app, pilot, pk: int):
    """Move the cursor onto a given PK.

    The node list sorts excepted/killed to the top, so row 0 is not
    necessarily the workchain a test cares about.
    """
    for index in range(app.table.row_count):
        if int(app.table.get_row_at(index)[0]) == pk:
            app.table.move_cursor(row=index)
            await pilot.pause()
            return
    raise AssertionError(f"PK {pk} not in the table")


def _title(app) -> str:
    return str(app.title_widget.content)


# --------------------------------------------------------------------------- #


async def test_group_list_then_nodes(app):
    async with app.run_test() as pilot:
        assert app.mode == "groups"
        assert app.table.row_count == 1

        await _enter_group(pilot)
        assert app.mode == "nodes"
        assert app.table.row_count == 2


async def test_breadcrumb_includes_the_group_label(app):
    """Mode used to be set after load_nodes, so the breadcrumb said just 'Groups'."""
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        assert "electrides" in app._base_title


async def test_nodes_list_is_populated_after_entering_a_group(app):
    """_apply_search_filter used to clobber nodes_list to [] during the load."""
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        assert app.nodes_list == [101, 100] or set(app.nodes_list) == {100, 101}


async def test_drilling_into_a_workchain_while_tag_filtered(app):
    """The reported bug: T then a showed nothing.

    The tag filter was re-applied to descendants, but only group-member
    fathers are ever tagged, so every child row was filtered away.
    """
    async with app.run_test() as pilot:
        await _enter_group(pilot)

        await pilot.press("T")  # all -> tagged
        await pilot.pause()
        assert app._tag_filter == "tagged"
        assert app.table.row_count == 1  # only WC 100 is tagged

        await pilot.press("a")
        await pilot.pause()

        assert app.mode == "descendants"
        assert app.table.row_count == 1, "descendants table must not be filtered away"
        assert app.nodes_list == [200]


async def test_tag_filter_cycles_and_reports_an_empty_result(app):
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        app.tags = {}  # nothing tagged at all
        await pilot.press("T")
        await pilot.pause()
        assert app.table.row_count == 0
        # The title must explain why the table is empty.
        assert "0 of 2" in _title(app)


async def test_select_on_an_empty_table_does_not_crash(app):
    """cursor_row is 0 on an empty table, so the old None guard never fired."""
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        app.tags = {}
        await pilot.press("T")
        await pilot.pause()
        assert app.table.row_count == 0

        await pilot.press("a")
        await pilot.pause()
        assert app.mode == "nodes"  # still alive, no exception


async def test_full_drill_down_and_back(app):
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        await _select_pk(app, pilot, 100)
        await pilot.press("a")  # WC 100 -> descendants
        await pilot.pause()
        assert app.mode == "descendants"

        await pilot.press("a")  # WC 200 -> descendants (CalcJob 300)
        await pilot.pause()
        assert app.mode == "descendants"

        await pilot.press("a")  # CalcJob -> file list
        await pilot.pause()
        assert app.mode == "file_list"
        assert app.table.row_count == 2  # aiida.out AND CRASH

        await pilot.press("a")  # file -> content
        await pilot.pause()
        assert app.mode == "file_view"

        for expected in ("file_list", "descendants", "descendants", "nodes"):
            await pilot.press("b")
            await pilot.pause()
            assert app.mode == expected


async def test_crash_file_is_listed(app):
    """The file list was hardcoded to three names, hiding QE's CRASH file."""
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        await _select_pk(app, pilot, 101)  # WC 101 -> CalcJob 301 directly
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert app.mode == "file_list"
        names = [app.table.get_row_at(i)[0] for i in range(app.table.row_count)]
        assert "CRASH" in names


async def test_calcjob_with_no_files_does_not_push_navigation_state(app, monkeypatch):
    monkeypatch.setattr(app_module.node_inspector, "list_all_files", lambda node: [])
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        await _select_pk(app, pilot, 101)
        await pilot.press("a")  # -> descendants (CalcJob 301)
        await pilot.pause()
        depth_before = len(app.navigation_stack)
        await pilot.press("a")  # CalcJob with no files
        await pilot.pause()
        assert app.mode == "file_list"
        assert len(app.navigation_stack) == depth_before + 1
        assert "never have run" in app._base_title


async def test_multi_tag_renders_every_tag(app):
    app.tags = {100: {"scf", "mpich"}}
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        row = app.table.get_row_at(0)
        cell = str(row[5])
        # Row order puts the excepted node first; find the tagged one.
        cells = [str(app.table.get_row_at(i)[5]) for i in range(app.table.row_count)]
        joined = " ".join(cells)
        assert "scf" in joined and "mpich" in joined


async def test_help_screen_opens(app):
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, app_module.HelpScreen)


async def test_tag_filter_is_rejected_outside_the_nodes_list(app):
    async with app.run_test() as pilot:
        await _enter_group(pilot)
        await _select_pk(app, pilot, 100)
        await pilot.press("a")
        await pilot.pause()
        assert app.mode == "descendants"
        await pilot.press("T")
        await pilot.pause()
        assert app._tag_filter == "all"  # unchanged
        assert app.table.row_count == 1
