#!/usr/bin/env python
"""
Simple AiiDA TUI: list nodes in a given Group using Textual.

Usage:
    python aiida_group_nodes_tui.py GROUP_IDENTIFIER

where GROUP_IDENTIFIER can be:
  - group label
  - group PK
  - group UUID
"""

from __future__ import annotations

import sys
from typing import Optional

from aiida.manage.configuration import load_profile
from aiida import orm
from aiida.orm import load_group, Node
from aiida.common.exceptions import NotExistent

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static
from textual.binding import Binding


class GroupNodesApp(App):
    """Textual app that displays nodes in a given AiiDA group."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #title {
        height: 3;
        content-align: center middle;
    }

    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("a", "select", "Select", show=True),
        Binding("b", "go_back", "Back", show=True),
    ]

    def __init__(self, group_identifier: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.group_identifier = group_identifier
        self.group = None
        self.table: Optional[DataTable] = None
        self.title_widget: Optional[Static] = None

        self.mode = "groups"  # "groups" or "nodes"
        self.groups = []  # list of group dicts from get_groups()

    def compose(self) -> ComposeResult:
        yield Header()
        self.title_widget = Static(id="title")
        yield self.title_widget
        self.table = DataTable(zebra_stripes=True)
        yield self.table
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app is ready; load group and nodes."""
        self.show_group_list()

    def show_group_list(self) -> None:
        """Populate the table with available groups."""
        assert self.table is not None

        self.mode = "groups"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"

        # Define columns for the groups view
        self.table.add_columns("Label", "Type", "#Nodes")

        # Fetch groups from AiiDA
        self.groups = get_groups()

        for g in self.groups:
            self.table.add_row(g["label"], g["type_string"], str(g["n_nodes"]))

        # Update title
        if self.title_widget is not None:
            self.title_widget.update("[b]Select a group to analyse[/b]")

        # Focus first row if any groups
        if self.groups:
            self.table.focus()
            self.table.cursor_coordinate = (0, 0)

    def load_group(self) -> None:
        """Load the AiiDA group given the identifier."""

        try:
            self.group = load_group(self.group_identifier)
        except NotExistent:
            # Show an error message and exit the app.
            self.console.print(f"[red]Group not found:[/red] {self.group_identifier}")
            self.exit(1)

    def setup_table(self) -> None:
        """Configure the DataTable columns."""
        assert self.table is not None

        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns(
            "PK", "UUID", "Type/Formula", "Process state", "Exit code"
        )

    def load_nodes(self) -> None:
        """Load nodes from the group using QueryBuilder for speed."""
        assert self.table is not None
        assert self.group is not None

        self.table.clear()
        qb = orm.QueryBuilder()

        qb.append(
            orm.Group,
            filters={"label": self.group.label},
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

        results = qb.all()

        for (
            pk,
            uuid,
            node_type,
            formula,
            process_label,
            process_state,
            exit_status,
        ) in results:

            short_uuid = uuid[:8]

            if "StructureData" in node_type:

                row_type = formula or "Structure"
                row_state = "-"
                row_exit = "-"
            else:
                # Process row
                row_type = process_label
                row_state = process_state if process_state else "-"
                row_exit = exit_status if exit_status is not None else "-"

            self.table.add_row(
                str(pk),
                short_uuid,
                row_type,
                row_state,
                row_exit,
            )

        # update title
        if self.title_widget is not None:
            self.title_widget.update(
                f"[b]Group:[/b] {self.group.label or self.group_identifier} | "
                f"[b]Nodes:[/b] {len(results)}"
            )

        if results:
            self.table.focus()
            self.table.cursor_coordinate = (0, 0)

    def action_refresh(self) -> None:
        """Reload the current view (groups or nodes)."""
        if self.mode == "groups":
            self.show_group_list()
        else:
            self.load_nodes()

    def action_quit(self) -> None:
        """Quit the app (bound to 'q')."""
        self.exit()

    def action_select(self) -> None:
        """Handle Enter: either select a group or (later) act on a node."""
        assert self.table is not None

        row_index = self.table.cursor_row
        if row_index is None:
            return
        print(f"Selected row: {row_index}")

        if self.mode == "groups":
            # Get the currently selected row (group)
            row = self.table.get_row_at(row_index)
            group_label = row[0]  # first column is Label
            self.log(f"Selected row index={row_index}, row={row}")

            # Store and switch to node view
            self.group_identifier = group_label
            self.load_group()
            self.setup_table()  # existing node-table setup
            self.load_nodes()  # existing node loading
            self.mode = "nodes"

        else:
            # mode == "nodes": later this can show node details, etc.
            pass

    def action_go_back(self) -> None:
        """Go back from nodes view to the group list."""
        # Only makes sense when we're currently looking at nodes
        if self.mode != "nodes":
            return

        # Remember which group we were on (label)
        current_group_label = None
        if self.group is not None and self.group.label:
            current_group_label = self.group.label
        elif self.group_identifier:
            current_group_label = self.group_identifier

        # Show the group list again
        self.show_group_list()  # This sets mode="groups" and repopulates self.groups

        # If we know which group we came from, re-select it in the list
        if current_group_label and self.groups:
            assert self.table is not None
            for idx, g in enumerate(self.groups):
                if g["label"] == current_group_label:
                    self.table.cursor_coordinate = (idx, 0)
                    break

        # Ensure mode is groups (show_group_list already does this, but be explicit)
        self.mode = "groups"


def get_groups():
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


def main() -> None:
    load_profile()
    app = GroupNodesApp()
    app.run()


if __name__ == "__main__":
    main()
