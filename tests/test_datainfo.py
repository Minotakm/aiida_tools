"""Data-node provenance in the tree, and the inspector panel."""

from __future__ import annotations

import pytest

pytest.importorskip("aiida")

from aiida import orm  # noqa: E402
from aiida.common.links import LinkType  # noqa: E402

from aiida_error_inspector import datainfo, traversal  # noqa: E402

pytestmark = pytest.mark.db


def _structure(cell, sites, label=""):
    node = orm.StructureData(cell=cell)
    for symbol, position in sites:
        node.append_atom(position=position, symbols=symbol)
    if label:
        node.label = label
    return node


@pytest.fixture
def provenance(aiida_profile_clean):
    """A workchain with a real input structure, parameters and an output structure."""
    cell = [[3.0, 0.0, 0.0], [0.0, 3.0, 0.0], [0.0, 0.0, 3.0]]
    inp = _structure(cell, [("Ca", (0, 0, 0)), ("Ca", (1.5, 1.5, 0)), ("N", (1.5, 0, 1.5))])
    inp.store()
    out = _structure(cell, [("Ca", (0, 0, 0)), ("Ca", (1.5, 1.5, 0)), ("N", (1.5, 0, 1.6))])

    params = orm.Dict(dict={"CONTROL": {"calculation": "relax"}, "SYSTEM": {"ecutwfc": 60.0}})
    params.store()

    kpoints = orm.KpointsData()
    kpoints.set_kpoints_mesh([4, 4, 4], offset=[0, 0, 0])
    kpoints.store()

    wc = orm.WorkChainNode()
    wc.base.attributes.set("process_label", "PwRelaxWorkChain")
    wc.base.attributes.set("process_state", "finished")
    wc.base.attributes.set("exit_status", 401)
    wc.base.links.add_incoming(inp, link_type=LinkType.INPUT_WORK, link_label="structure")
    wc.base.links.add_incoming(params, link_type=LinkType.INPUT_WORK, link_label="parameters")
    wc.base.links.add_incoming(kpoints, link_type=LinkType.INPUT_WORK, link_label="kpoints")
    wc.store()

    # A RETURN target must already be stored, so store first and link after.
    out.store()
    out.base.links.add_incoming(wc, link_type=LinkType.RETURN, link_label="output_structure")

    calc = orm.CalcJobNode()
    calc.base.attributes.set("process_label", "PwCalculation")
    calc.base.attributes.set("process_state", "finished")
    calc.base.attributes.set("exit_status", 305)
    calc.base.links.add_incoming(wc, link_type=LinkType.CALL_CALC, link_label="iteration_01")
    calc.base.links.add_incoming(inp, link_type=LinkType.INPUT_CALC, link_label="structure")
    calc.store()

    for node in (calc, wc):
        node.seal()
    return {"wc": wc.pk, "calc": calc.pk, "in": inp.pk, "out": out.pk,
            "params": params.pk, "kpoints": kpoints.pk}


# --------------------------------------------------------------------------- #
# The provenance query
# --------------------------------------------------------------------------- #


def test_data_links_finds_inputs_and_outputs(provenance):
    links = traversal.data_links([provenance["wc"]])
    refs = links[provenance["wc"]]

    inputs = {r.link_label: r for r in refs if r.direction == "input"}
    outputs = {r.link_label: r for r in refs if r.direction == "output"}

    assert set(inputs) == {"structure", "parameters", "kpoints"}
    assert set(outputs) == {"output_structure"}
    assert inputs["structure"].pk == provenance["in"]
    assert outputs["output_structure"].pk == provenance["out"]


def test_structure_refs_are_recognised(provenance):
    links = traversal.data_links([provenance["wc"]])
    by_label = {r.link_label: r for r in links[provenance["wc"]]}
    assert by_label["structure"].is_structure
    assert by_label["structure"].kind == "StructureData"
    assert not by_label["parameters"].is_structure
    assert by_label["parameters"].kind == "Dict"


def test_data_links_is_batched_across_processes(provenance):
    links = traversal.data_links([provenance["wc"], provenance["calc"]])
    assert set(links) == {provenance["wc"], provenance["calc"]}
    calc_inputs = [r for r in links[provenance["calc"]] if r.direction == "input"]
    assert [r.link_label for r in calc_inputs] == ["structure"]


def test_inputs_sort_before_outputs(provenance):
    refs = traversal.data_links([provenance["wc"]])[provenance["wc"]]
    directions = [r.direction for r in refs]
    assert directions == sorted(directions, key=lambda d: d != "input")


def test_empty_input_is_safe():
    assert traversal.data_links([]) == {}


def test_elements_fall_back_when_formula_hill_is_absent(provenance):
    """core AiiDA does not set extras.formula_hill; MC3D-style databases do."""
    ref = next(
        r
        for r in traversal.data_links([provenance["wc"]])[provenance["wc"]]
        if r.is_structure
    )
    assert ref.formula is None
    assert ref.elements == ["Ca", "N"]
    assert ref.summary() == "Ca, N"


def test_formula_hill_is_preferred_when_present(provenance):
    orm.load_node(provenance["in"]).base.extras.set("formula_hill", "Ca2N")
    ref = next(
        r
        for r in traversal.data_links([provenance["wc"]])[provenance["wc"]]
        if r.is_structure
    )
    assert ref.summary() == "Ca2N"


# --------------------------------------------------------------------------- #
# The inspector
# --------------------------------------------------------------------------- #


def test_structure_description(provenance):
    node = orm.load_node(provenance["in"])
    text = datainfo.describe(node)

    assert f"PK {node.pk}" in text
    assert "Formula" in text
    assert "Ca2N" in text
    assert "Sites      3 atoms" in text
    assert "Elements   Ca, N" in text
    assert "27.000 Å³" in text          # 3x3x3 cell
    assert "Density" in text
    assert "Cell" in text
    assert "a, b, c" in text
    # Site table with fractional-free cartesian positions.
    assert "kind" in text
    assert text.count("Ca") >= 2


def test_structure_description_lists_every_site(provenance):
    node = orm.load_node(provenance["in"])
    text = datainfo.describe(node)
    assert "Sites (3)" in text


def test_dict_description(provenance):
    text = datainfo.describe(orm.load_node(provenance["params"]))
    assert "Keys       2" in text
    assert "ecutwfc" in text
    assert "60.0" in text
    assert "calculation" in text


def test_kpoints_description(provenance):
    text = datainfo.describe(orm.load_node(provenance["kpoints"]))
    assert "Mesh       4 x 4 x 4" in text


def test_extras_are_surfaced(aiida_profile_clean):
    """Extras usually carry the source-database IDs that identify a material."""
    node = _structure([[2, 0, 0], [0, 2, 0], [0, 0, 2]], [("Si", (0, 0, 0))])
    node.store()
    node.base.extras.set("source_id", "mc3d-1234")
    text = datainfo.describe(orm.load_node(node.pk))
    assert "Extras" in text
    assert "source_id" in text
    assert "mc3d-1234" in text


def test_generic_data_node_still_describes(aiida_profile_clean):
    node = orm.Int(42).store()
    text = datainfo.describe(orm.load_node(node.pk))
    assert "Value      42" in text


def test_describe_never_raises(aiida_profile_clean):
    class Broken:
        pk = 1
        uuid = "x"
        label = ""
        description = ""

        def __getattr__(self, item):
            raise RuntimeError("boom")

    text = datainfo.describe(Broken())
    assert "could not summarise" in text or "PK" in text


# --------------------------------------------------------------------------- #
# The tree, driven through the real app
# --------------------------------------------------------------------------- #


@pytest.fixture
def app(provenance, tmp_path):
    pytest.importorskip("textual")
    from aiida_error_inspector.app import GroupNodesApp

    instance = GroupNodesApp(data_dir=tmp_path)
    instance.root_node = orm.load_node(provenance["wc"])
    instance.current_node = instance.root_node
    return instance


async def _settle(pilot, app):
    for _ in range(40):
        await pilot.pause()
        if app.workflow_tree is not None and app.workflow_tree.root.children:
            return
    await pilot.pause()


def _branch(node, name):
    for child in node.children:
        if str(child.label).startswith(name):
            return child
    raise AssertionError(f"no {name!r} branch in {[str(c.label) for c in node.children]}")


async def test_tree_hangs_inputs_and_outputs_off_the_process(app, provenance):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        root = app.workflow_tree.root
        inputs = _branch(root, "inputs")
        outputs = _branch(root, "outputs")

        assert "(3)" in str(inputs.label)
        assert {n.data.pk for n in inputs.children} == {
            provenance["in"], provenance["params"], provenance["kpoints"]
        }
        assert [n.data.pk for n in outputs.children] == [provenance["out"]]


async def test_structure_label_shows_the_formula(app, provenance):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        inputs = _branch(app.workflow_tree.root, "inputs")
        labels = {str(n.label) for n in inputs.children}
        assert any("StructureData" in text and "structure:" in text for text in labels)
        assert any("Dict" in text for text in labels)


async def test_data_branches_start_collapsed(app):
    """The call graph should stay as readable as `verdi process status`."""
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        root = app.workflow_tree.root
        assert root.is_expanded
        assert not _branch(root, "inputs").is_expanded


async def test_toggling_data_nodes_off_leaves_only_processes(app, provenance):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)
        assert app.show_data_nodes

        await pilot.press("D")
        for _ in range(40):
            await pilot.pause()
            if not any(str(c.label).startswith("inputs") for c in app.workflow_tree.root.children):
                break

        assert not app.show_data_nodes
        labels = [str(c.label) for c in app.workflow_tree.root.children]
        assert not any(text.startswith("inputs") for text in labels)
        assert any("PwCalculation" in text for text in labels)


async def test_selecting_a_structure_opens_the_inspector(app, provenance):
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = "descendants"
        await pilot.press("w")
        await _settle(pilot, app)

        inputs = _branch(app.workflow_tree.root, "inputs")
        inputs.expand()
        await pilot.pause()
        structure = next(n for n in inputs.children if n.data.pk == provenance["in"])

        app.workflow_tree.select_node(structure)
        app.workflow_tree.action_select_cursor()
        for _ in range(30):
            await pilot.pause()
            if app.mode == "panel":
                break

        assert app.mode == "panel"
        assert "Ca2N" in app.detail_view.text
        assert "Sites" in app.detail_view.text
