"""AiiDA query functions for the TUI."""

from __future__ import annotations

from aiida import orm
from aiida.orm import Node


def get_groups() -> list[dict]:
    """Get all core groups from AiiDA database.

    Returns:
        List of dicts with keys: label, type_string, n_nodes
    """
    qb = orm.QueryBuilder()
    qb.append(
        orm.Group,
        project=["label", "type_string", "*"],
        filters={"type_string": "core"},
    )

    rows = qb.all()
    return [
        {"label": label, "type_string": type_string, "n_nodes": len(group.nodes)}
        for label, type_string, group in rows
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


def get_descendants(node: Node) -> list[Node]:
    """Load all called descendants of a given node.

    Args:
        node: Parent node

    Returns:
        List of descendant nodes
    """
    qb = orm.QueryBuilder()
    qb.append(
        orm.Node,
        filters={"id": node.id},
        tag="parent",
    )
    qb.append(
        orm.Node,
        with_incoming="parent",
        project=["*"],
    )
    descendants = [row[0] for row in qb.all()]

    return descendants
