"""AiiDA query functions for the TUI."""

from __future__ import annotations

from aiida import orm
from aiida.orm import Node


def get_groups() -> list[dict]:
    """Get all core groups from AiiDA database.

    Returns:
        List of dicts with keys: label, type_string, n_nodes
    """
    # Single query for group metadata; preserves empty groups
    qb = orm.QueryBuilder()
    qb.append(
        orm.Group,
        project=["id", "label", "type_string"],
        filters={"type_string": "core"},
    )
    groups = qb.all()

    # Single query that streams (group_id, node_id) pairs across all core groups.
    # Replaces the per-group group.count() N+1 with one DB roundtrip.
    counts: dict[int, int] = {}
    count_qb = orm.QueryBuilder()
    count_qb.append(
        orm.Group,
        filters={"type_string": "core"},
        project=["id"],
        tag="g",
    )
    count_qb.append(orm.Node, with_group="g", project=["id"])
    for group_id, _ in count_qb.iterall():
        counts[group_id] = counts.get(group_id, 0) + 1

    return [
        {"label": label, "type_string": type_string, "n_nodes": counts.get(gid, 0)}
        for gid, label, type_string in groups
    ]


def get_nodes_in_group(group_label: str) -> list[tuple]:
    """Get all nodes in a given group.

    Args:
        group_label: Label of the group

    Returns:
        List of tuples: (pk, uuid, node_type, formula, process_label,
                        process_state, exit_status)
    """
    qb = orm.QueryBuilder()

    qb.append(
        orm.Group,
        filters={"label": group_label},
        tag="group_tag",
    )

    qb.append(
        orm.Node,
        with_group="group_tag",
        project=[
            "pk",
            "uuid",
            "node_type",
            "extras.formula_hill",
            "attributes.process_label",
            "attributes.process_state",
            "attributes.exit_status",
        ],
    )

    return qb.all()


def get_descendants(node: Node) -> list[dict]:
    """Directly called WorkChains and CalcJobs of a node.

    Projects the handful of attributes the table needs instead of
    ``project=["*"]``. The previous version materialised a full ORM instance per
    row and then read three attributes off it.
    """
    qb = orm.QueryBuilder()
    qb.append(orm.Node, filters={"id": node.pk}, tag="parent")
    qb.append(
        orm.Node,
        with_incoming="parent",
        filters={
            "node_type": {
                "or": [
                    {"like": "process.workflow.workchain.%"},
                    {"like": "process.calculation.calcjob.%"},
                ]
            }
        },
        project=[
            "id",
            "node_type",
            "attributes.process_label",
            "attributes.process_state",
            "attributes.exit_status",
        ],
        tag="child",
    )

    return [
        {
            "pk": pk,
            "node_type": node_type or "",
            "process_label": process_label,
            "process_state": process_state,
            "exit_status": exit_status,
        }
        for pk, node_type, process_label, process_state, exit_status in qb.all()
    ]
