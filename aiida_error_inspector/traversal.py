"""Walking the call graph from a father workchain down to the calculations that failed.

The previous implementation hardcoded exactly one intermediate workchain
(father -> child WorkChain -> CalcJob), so only depth-2 topologies resolved.
Measured on a synthetic call graph, a direct father->CalcJob and a
father->WC->WC->CalcJob both returned nothing — which is what put ~48% of the
user's failed workchains permanently out of reach.

``with_ancestors`` is **not** the fix: QueryBuilder's recursive join follows only
``CREATE`` and ``INPUT_CALC`` links (``joiner.py:402,469``), and a workchain emits
``CALL_CALC``/``CALL_WORK``/``RETURN``, so it returns nothing at any depth. What
works is an explicit breadth-first walk with ``edge_filters`` pinned to the call
links — batched across every father at once, so the whole group costs a handful of
queries rather than one per workchain per pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from aiida import orm
from aiida.common.links import LinkType

logger = logging.getLogger(__name__)

CALL_LINKS = [LinkType.CALL_CALC.value, LinkType.CALL_WORK.value]

#: Processes that failed, however they failed.
#:
#: ``exit_status > 0`` rather than ``{"!==": 0}``: the comparison is type-guarded
#: (``main.py:667``) so an absent key falls to ``ELSE false`` and is excluded from
#: this branch cleanly. The ``process_state`` branch catches excepted/killed
#: independently. (``{"!==": 0}`` does in fact match absent keys — it compiles to
#: ``NOT FALSE`` — but relying on that reads like a bug.)
FAILED_PROCESS_FILTER: dict[str, Any] = {
    "or": [
        {"attributes.process_state": {"in": ["excepted", "killed"]}},
        {
            "and": [
                {"attributes.process_state": "finished"},
                {"attributes.exit_status": {">": 0}},
            ]
        },
    ]
}

_PROCESS_PROJECTION = [
    "id",
    "node_type",
    "attributes.process_label",
    "attributes.process_state",
    "attributes.exit_status",
    "ctime",
    "attributes.exit_message",
    "attributes.stepper_state_info",
    "attributes.process_status",
]


@dataclass(frozen=True)
class ProcessRef:
    """A process identified by projection only — no ORM instance loaded.

    The old code used ``project=["*"]`` and then touched ``.process_state`` etc.
    on each row, instantiating a full ORM object per node purely to read three
    attributes.
    """

    pk: int
    node_type: str
    process_label: str | None = None
    process_state: str | None = None
    exit_status: int | None = None
    ctime: datetime | None = None
    depth: int = 0
    root_pk: int | None = None
    #: Set while walking the call graph, so a flat list can be re-nested.
    parent_pk: int | None = None
    #: The ``CALL_*`` link label, e.g. "iteration_01".
    call_link: str | None = None
    exit_message: str | None = None
    #: WorkChain only: the "[1:while_(should_run)(2:inspect)]" fragment that
    #: ``verdi process status`` shows.
    stepper_state_info: str | None = None
    process_status: str | None = None

    @property
    def is_calcjob(self) -> bool:
        return self.node_type.startswith("process.calculation.calcjob")

    @property
    def is_workchain(self) -> bool:
        return self.node_type.startswith("process.workflow.workchain")

    @property
    def failed(self) -> bool:
        if self.process_state in ("excepted", "killed"):
            return True
        return self.process_state == "finished" and bool(self.exit_status)

    @property
    def label(self) -> str:
        if self.process_label:
            return self.process_label
        # "process.calculation.calcjob.CalcJobNode." -> "CalcJobNode"
        return self.node_type.rstrip(".").rsplit(".", 1)[-1] or self.node_type

    @classmethod
    def from_row(
        cls,
        row: Sequence[Any],
        *,
        depth: int = 0,
        root_pk: int | None = None,
        parent_pk: int | None = None,
        call_link: str | None = None,
    ) -> "ProcessRef":
        (
            pk,
            node_type,
            process_label,
            process_state,
            exit_status,
            ctime,
            exit_message,
            stepper_state_info,
            process_status,
        ) = row
        return cls(
            pk=pk,
            node_type=node_type or "",
            process_label=process_label,
            process_state=process_state,
            exit_status=exit_status,
            ctime=ctime,
            depth=depth,
            root_pk=root_pk,
            parent_pk=parent_pk,
            call_link=call_link,
            exit_message=exit_message,
            stepper_state_info=stepper_state_info,
            process_status=process_status,
        )

    def status_line(self) -> str:
        """One line in the style of ``verdi process status``.

        e.g. ``PwBaseWorkChain<157620> Finished [410] [1:while_(run)(2:inspect)]``
        """
        state = (self.process_state or "None").capitalize()
        text = f"{self.label}<{self.pk}>"
        if self.call_link and self.call_link not in ("CALL", "CALL_CALC", "CALL_WORK"):
            text = f"{self.label}<{self.pk} | {self.call_link}>"
        text += f" {state}"
        if self.exit_status is not None:
            text += f" [{self.exit_status}]"
        if self.stepper_state_info:
            text += f" [{self.stepper_state_info}]"
        return text


# --------------------------------------------------------------------------- #
# Father selection
# --------------------------------------------------------------------------- #


def failed_workchains_in_group(group_label: str) -> list[ProcessRef]:
    """Every failed workchain that is a member of the group.

    Replaces the query at ``app.py:1495-1509``, which required
    ``process_state == "finished"`` and so could never return an excepted or
    killed workchain — precisely the ones the node list sorts to the top as most
    severe.
    """
    qb = orm.QueryBuilder()
    qb.append(orm.Group, filters={"label": group_label}, tag="group")
    qb.append(
        orm.WorkChainNode,
        with_group="group",
        filters=FAILED_PROCESS_FILTER,
        project=_PROCESS_PROJECTION,
        tag="wc",
    )
    return [ProcessRef.from_row(row) for row in qb.all()]


# --------------------------------------------------------------------------- #
# The call-graph walk
# --------------------------------------------------------------------------- #


def call_forest(
    root_pks: Sequence[int],
    *,
    max_depth: int = 8,
    chunk: int = 500,
) -> dict[int, list[ProcessRef]]:
    """Map each root pk to every process reachable through ``CALL_*`` links.

    Breadth-first and batched: one query per level per chunk of the frontier,
    for *all* roots simultaneously. A ``seen`` set makes a diamond in the call
    graph safe.
    """
    forest: dict[int, list[ProcessRef]] = {pk: [] for pk in root_pks}
    if not root_pks:
        return forest

    # Which root did we reach this node from? Roots map to themselves.
    owner: dict[int, int] = {pk: pk for pk in root_pks}
    seen: set[int] = set(root_pks)
    frontier: list[int] = list(root_pks)

    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        next_frontier: list[int] = []

        for start in range(0, len(frontier), chunk):
            batch = frontier[start : start + chunk]
            qb = orm.QueryBuilder()
            qb.append(orm.ProcessNode, filters={"id": {"in": batch}}, project=["id"], tag="parent")
            qb.append(
                orm.ProcessNode,
                with_incoming="parent",
                edge_filters={"type": {"in": CALL_LINKS}},
                edge_project=["label"],
                project=_PROCESS_PROJECTION,
                tag="child",
            )
            for row in qb.all():
                parent_pk = row[0]
                child_row = row[1 : 1 + len(_PROCESS_PROJECTION)]
                call_link = row[1 + len(_PROCESS_PROJECTION)]
                child_pk = child_row[0]
                if child_pk in seen:
                    continue
                seen.add(child_pk)

                root_pk = owner.get(parent_pk)
                if root_pk is None:
                    continue
                owner[child_pk] = root_pk
                forest[root_pk].append(
                    ProcessRef.from_row(
                        child_row,
                        depth=depth,
                        root_pk=root_pk,
                        parent_pk=parent_pk,
                        call_link=call_link,
                    )
                )
                next_frontier.append(child_pk)

        frontier = next_frontier

    if frontier:
        logger.warning(
            "Call graph deeper than max_depth=%s; %s nodes not explored",
            max_depth,
            len(frontier),
        )
    return forest


def select_candidate_calcjobs(
    descendants: Iterable[ProcessRef],
    *,
    limit: int = 5,
    only_failed: bool = True,
) -> list[ProcessRef]:
    """Failing CalcJobs worth inspecting, deepest first then newest first.

    The old code took the single most recent CalcJob (``limit(1)`` on ctime at
    ``app.py:1910-1911``). That is wrong for restart-driven workchains: a
    ``PwBaseWorkChain`` reruns after e.g. a 305, so the newest CalcJob is often
    not the one that characterises the failure.
    """
    candidates = [d for d in descendants if d.is_calcjob and (d.failed or not only_failed)]
    # Deepest first; within a depth, newest first.
    candidates.sort(key=lambda d: (-d.depth, -(d.ctime.timestamp() if d.ctime else 0.0)))
    return candidates[:limit] if limit else candidates


def load_calcjobs(pks: Sequence[int]) -> dict[int, orm.CalcJobNode]:
    """Batch-load ORM instances, only for the CalcJobs we will actually read."""
    if not pks:
        return {}
    qb = orm.QueryBuilder()
    qb.append(orm.CalcJobNode, filters={"id": {"in": list(pks)}}, project=["*"])
    return {node.pk: node for node in qb.all(flat=True)}


def process_refs(pks: Sequence[int]) -> dict[int, ProcessRef]:
    """Projected refs for specific processes, without loading ORM instances."""
    if not pks:
        return {}
    qb = orm.QueryBuilder()
    qb.append(
        orm.ProcessNode,
        filters={"id": {"in": list(pks)}},
        project=_PROCESS_PROJECTION,
    )
    return {row[0]: ProcessRef.from_row(row) for row in qb.all()}


def children_by_parent(descendants: Iterable[ProcessRef]) -> dict[int, list[ProcessRef]]:
    """Re-nest the flat walk, children ordered as they were called.

    ``call_forest`` returns a flat list because that is what a scan wants; a
    tree view wants the shape back.
    """
    children: dict[int, list[ProcessRef]] = {}
    for ref in descendants:
        if ref.parent_pk is not None:
            children.setdefault(ref.parent_pk, []).append(ref)
    for refs in children.values():
        refs.sort(key=lambda r: (r.ctime or datetime.min, r.pk))
    return children


def call_tree(root_pk: int, *, max_depth: int = 8) -> tuple[ProcessRef | None, dict[int, list[ProcessRef]]]:
    """(root ref, children-by-parent) for one process — the `verdi process status` shape."""
    root = process_refs([root_pk]).get(root_pk)
    descendants = call_forest([root_pk], max_depth=max_depth).get(root_pk, [])
    return root, children_by_parent(descendants)


# --------------------------------------------------------------------------- #
# Data provenance: what went into and came out of each process
# --------------------------------------------------------------------------- #

INPUT_LINKS = [LinkType.INPUT_CALC.value, LinkType.INPUT_WORK.value]
OUTPUT_LINKS = [LinkType.CREATE.value, LinkType.RETURN.value]

_DATA_PROJECTION = [
    "id",
    "node_type",
    "label",
    "attributes.value",
    "extras.formula_hill",
    # Bounded by the number of distinct kinds, not sites, so this is cheap and
    # gives an element list when ``formula_hill`` is absent (core AiiDA does not
    # set that extra; MC3D-style databases do).
    "attributes.kinds",
]


@dataclass(frozen=True)
class DataRef:
    """A data node hanging off a process, by projection only."""

    pk: int
    node_type: str
    node_label: str | None = None
    value: Any = None
    formula: str | None = None
    kinds: Any = None
    link_label: str | None = None
    direction: str = "input"  # or "output"

    @property
    def kind(self) -> str:
        """"data.core.structure.StructureData." -> "StructureData"."""
        return self.node_type.rstrip(".").rsplit(".", 1)[-1] or self.node_type

    @property
    def is_structure(self) -> bool:
        return "structure" in self.node_type.lower()

    def summary(self) -> str:
        """Short, type-appropriate description for a tree label."""
        if self.formula:
            return self.formula
        if self.value is not None:
            text = str(self.value)
            return text if len(text) <= 40 else text[:37] + "…"
        elements = self.elements
        if elements:
            return ", ".join(elements)
        return self.node_label or ""

    @property
    def elements(self) -> list[str]:
        """Distinct chemical symbols, from the projected kinds."""
        if not isinstance(self.kinds, list):
            return []
        symbols: list[str] = []
        for kind in self.kinds:
            if not isinstance(kind, dict):
                continue
            for symbol in kind.get("symbols") or ():
                if symbol not in symbols:
                    symbols.append(symbol)
        return symbols

    @classmethod
    def from_row(cls, row: Sequence[Any], *, link_label: str, direction: str) -> "DataRef":
        pk, node_type, node_label, value, formula, kinds = row
        return cls(
            pk=pk,
            node_type=node_type or "",
            node_label=node_label or None,
            value=value,
            formula=formula,
            kinds=kinds,
            link_label=link_label,
            direction=direction,
        )


def data_links(
    process_pks: Sequence[int], *, chunk: int = 500
) -> dict[int, list[DataRef]]:
    """Every data node linked to each process, both directions.

    Two batched queries per chunk rather than one per node. Inputs are found
    with ``with_outgoing`` (data -> process) and outputs with ``with_incoming``
    (process -> data).
    """
    links: dict[int, list[DataRef]] = {pk: [] for pk in process_pks}
    if not process_pks:
        return links

    pks = list(process_pks)
    for start in range(0, len(pks), chunk):
        batch = pks[start : start + chunk]
        for direction, link_types, relation in (
            ("input", INPUT_LINKS, "with_outgoing"),
            ("output", OUTPUT_LINKS, "with_incoming"),
        ):
            qb = orm.QueryBuilder()
            qb.append(
                orm.ProcessNode,
                filters={"id": {"in": batch}},
                project=["id"],
                tag="proc",
            )
            qb.append(
                orm.Data,
                **{relation: "proc"},
                edge_filters={"type": {"in": link_types}},
                edge_project=["label"],
                project=_DATA_PROJECTION,
                tag="data",
            )
            for row in qb.all():
                proc_pk = row[0]
                data_row = row[1 : 1 + len(_DATA_PROJECTION)]
                link_label = row[1 + len(_DATA_PROJECTION)]
                if proc_pk in links:
                    links[proc_pk].append(
                        DataRef.from_row(
                            data_row, link_label=link_label, direction=direction
                        )
                    )

    for refs in links.values():
        refs.sort(key=lambda r: (r.direction != "input", r.link_label or "", r.pk))
    return links
