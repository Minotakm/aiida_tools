"""The `verdi process status`-style call-graph panel."""

from __future__ import annotations

import io

import pytest

pytest.importorskip("aiida")
pytest.importorskip("textual")

from aiida import orm  # noqa: E402
from aiida.common.links import LinkType  # noqa: E402

from aiida_error_inspector import traversal  # noqa: E402

pytestmark = pytest.mark.db


def _wc(label, state="finished", exit_status=None, stepper=None, exit_message=None):
    node = orm.WorkChainNode()
    node.base.attributes.set("process_label", label)
    node.base.attributes.set("process_state", state)
    if exit_status is not None:
        node.base.attributes.set("exit_status", exit_status)
    if stepper is not None:
        node.base.attributes.set("stepper_state_info", stepper)
    if exit_message is not None:
        node.base.attributes.set("exit_message", exit_message)
    return node


def _calc(label="PwCalculation", state="finished", exit_status=None):
    node = orm.CalcJobNode()
    node.base.attributes.set("process_label", label)
    node.base.attributes.set("process_state", state)
    if exit_status is not None:
        node.base.attributes.set("exit_status", exit_status)
    return node


@pytest.fixture
def workflow(aiida_profile_clean):
    """A relax workflow that restarted once, like a real PwBaseWorkChain."""
    root = _wc(
        "PwRelaxWorkChain",
        "finished",
        401,
        stepper="1:while_(should_run_relax)(1:inspect_relax)",
        exit_message="the relax sub process failed",
    ).store()

    base = _wc("PwBaseWorkChain", "finished", 410, stepper="1:while_(should_run_process)")
    base.base.links.add_incoming(root, link_type=LinkType.CALL_WORK, link_label="relax")
    base.store()

    first = _calc(exit_status=305)
    first.base.links.add_incoming(base, link_type=LinkType.CALL_CALC, link_label="iteration_01")
    first.store()

    second = _calc(exit_status=305)
    second.base.links.add_incoming(base, link_type=LinkType.CALL_CALC, link_label="iteration_02")
    second.store()

    for node in (first, second, base, root):
        node.seal()
    return {"root": root.pk, "base": base.pk, "first": first.pk, "second": second.pk}


# --------------------------------------------------------------------------- #
# Tree shape
# --------------------------------------------------------------------------- #


def test_call_tree_returns_the_nested_shape(workflow):
    root, children = traversal.call_tree(workflow["root"])

    assert root is not None and root.pk == workflow["root"]
    assert [c.pk for c in children[workflow["root"]]] == [workflow["base"]]
    assert [c.pk for c in children[workflow["base"]]] == [
        workflow["first"],
        workflow["second"],
    ]
    assert workflow["first"] not in children  # leaves have no entry


def test_children_are_ordered_as_called(workflow):
    _root, children = traversal.call_tree(workflow["root"])
    labels = [c.call_link for c in children[workflow["base"]]]
    assert labels == ["iteration_01", "iteration_02"]


def test_ref_carries_the_status_fields(workflow):
    root, children = traversal.call_tree(workflow["root"])
    assert root.stepper_state_info.startswith("1:while_")
    assert root.exit_message == "the relax sub process failed"
    base = children[workflow["root"]][0]
    assert base.parent_pk == workflow["root"]
    assert base.depth == 1


def test_status_line_matches_verdi_shape(workflow):
    root, children = traversal.call_tree(workflow["root"])
    assert root.status_line() == (
        f"PwRelaxWorkChain<{workflow['root']}> Finished [401] "
        "[1:while_(should_run_relax)(1:inspect_relax)]"
    )
    calc = children[workflow["base"]][0]
    assert calc.status_line() == (
        f"PwCalculation<{workflow['first']} | iteration_01> Finished [305]"
    )


def test_call_tree_on_a_leaf_is_empty(workflow):
    root, children = traversal.call_tree(workflow["first"])
    assert root.pk == workflow["first"]
    assert children == {}


def test_max_depth_limits_the_walk(workflow):
    _root, children = traversal.call_tree(workflow["root"], max_depth=1)
    assert workflow["base"] in [c.pk for c in children[workflow["root"]]]
    assert workflow["base"] not in children  # its children were not walked


# --------------------------------------------------------------------------- #
# Panel rendering, driven through the real app
# --------------------------------------------------------------------------- #


@pytest.fixture
def app(workflow, tmp_path):
    from aiida_error_inspector.app import GroupNodesApp

    instance = GroupNodesApp(data_dir=tmp_path)
    instance.root_node = orm.load_node(workflow["root"])
    instance.current_node = instance.root_node
    return instance


async def _settle(pilot, app):
    """Let the debounce timer and the worker finish."""
    for _ in range(40):
        await pilot.pause()
        if app.workflow_tree is not None and app.workflow_tree.root.children:
            return
    await pilot.pause()


async def test_panel_is_hidden_by_default(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.show_workflow
        assert not app.query_one("#workflow_pane").has_class("visible")


async def test_toggling_builds_the_tree(app, workflow):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        assert app.show_workflow
        assert app.query_one("#workflow_pane").has_class("visible")

        root = app.workflow_tree.root
        assert root.data.pk == workflow["root"]
        assert [n.data.pk for n in root.children] == [workflow["base"]]
        assert [n.data.pk for n in root.children[0].children] == [
            workflow["first"],
            workflow["second"],
        ]


async def test_node_labels_show_state_and_exit_code(app, workflow):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        label = str(app.workflow_tree.root.label)
        assert "PwRelaxWorkChain" in label
        assert str(workflow["root"]) in label
        assert "401" in label
        assert "while_" in label

        calc_label = str(app.workflow_tree.root.children[0].children[0].label)
        assert "iteration_01" in calc_label
        assert "305" in calc_label


async def test_header_reports_the_counts(app):
    from textual.widgets import Static

    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)
        header = str(app.query_one("#workflow_header", Static).content)
        assert "3 called" in header
        assert "3 failed" in header


async def test_toggle_off_hides_the_pane(app):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)
        await pilot.press("w")
        await pilot.pause()
        assert not app.show_workflow
        assert not app.query_one("#workflow_pane").has_class("visible")


async def test_preference_is_persisted(app, tmp_path):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

    import json

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["show_workflow"] is True


async def test_selecting_in_the_tree_opens_that_process(app, workflow):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        calc_node = app.workflow_tree.root.children[0].children[0]
        app.workflow_tree.select_node(calc_node)
        app.workflow_tree.action_select_cursor()
        for _ in range(20):
            await pilot.pause()
            if app.mode == "file_list":
                break

        assert app.mode == "file_list"
        assert app.current_node.pk == workflow["first"]
