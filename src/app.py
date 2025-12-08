"""Main TUI application for browsing AiiDA groups and nodes."""

from __future__ import annotations

from typing import Optional

from aiida import orm
from aiida.common.exceptions import NotExistent
from aiida.orm import Node, load_group
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from node_inspector import get_file_content, get_retrieved_files
from queries import get_descendants, get_groups, get_nodes_in_group


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

        RichLog {
            height: 1fr;
            border: solid green;
            background: $surface;
        }
        """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("a", "select", "Select", show=True),
        Binding("b", "go_back", "Back", show=True),
        Binding("+", "increase_preview", "More lines", show=True),
        Binding("-", "decrease_preview", "Fewer lines", show=True),
    ]

    def __init__(self, group_identifier: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.group_identifier = group_identifier
        self.group = None
        self.table: Optional[DataTable] = None
        self.title_widget: Optional[Static] = None

        self.mode = (
            "groups"  # "groups", "nodes", "descendants", "file_list", "file_view"
        )
        self.groups = []
        self.current_node: Optional[Node] = None
        self.nodes_list = []
        self.detail_view: Optional[RichLog] = None
        self.available_files = []  # List of available files
        self.current_file: Optional[str] = None  # Currently selected file

        # Settings - show last 500 lines by default for files
        self.preview_lines = 500

    def compose(self) -> ComposeResult:
        yield Header()
        self.title_widget = Static(id="title")
        yield self.title_widget
        with Vertical():
            self.table = DataTable(zebra_stripes=True)
            yield self.table
            self.detail_view = RichLog(highlight=True, markup=True)
            self.detail_view.display = False
            yield self.detail_view
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app is ready."""
        self.show_group_list()

    def show_group_list(self) -> None:
        """Populate the table with available groups."""
        assert self.table is not None

        self.mode = "groups"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Label", "Type", "#Nodes")

        self.groups = get_groups()

        for g in self.groups:
            self.table.add_row(g["label"], g["type_string"], str(g["n_nodes"]))

        if self.title_widget is not None:
            self.title_widget.update("[b]Select a group to analyse[/b]")

        if self.groups:
            self.table.focus()

    def load_group(self) -> None:
        """Load the AiiDA group given the identifier."""
        try:
            self.group = load_group(self.group_identifier)
        except NotExistent:
            self.console.print(f"[red]Group not found:[/red] {self.group_identifier}")
            self.exit(1)

    def setup_table(self) -> None:
        """Configure the DataTable columns for nodes."""
        assert self.table is not None
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns(
            "PK", "UUID", "Type/Formula", "Process state", "Exit code"
        )

    def load_nodes(self) -> None:
        """Load nodes from the group."""
        assert self.table is not None
        assert self.group is not None

        self.table.clear()
        results = get_nodes_in_group(self.group.label)

        self.nodes_list = [pk for pk, *_ in results]

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
                row_type = process_label
                row_state = process_state if process_state else "-"
                row_exit = exit_status if exit_status is not None else "-"

            self.table.add_row(str(pk), short_uuid, row_type, row_state, row_exit)

        if self.title_widget is not None:
            self.title_widget.update(
                f"[b]Group:[/b] {self.group.label or self.group_identifier} | "
                f"[b]Nodes:[/b] {len(results)}"
            )

        if results:
            self.table.focus()

    def show_descendants(self, node: Node) -> None:
        """Display called WorkChains and CalcJobs only."""
        assert self.table is not None

        self.mode = "descendants"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("PK", "Process", "State", "Exit code")

        descendants = get_descendants(node)
        self.nodes_list = []

        # Filter to show only WorkChains and CalcJobs
        for desc_node in descendants:
            # Only show process nodes (WorkChains and CalcJobs)
            if not isinstance(desc_node, (orm.WorkChainNode, orm.CalcJobNode)):
                continue

            self.nodes_list.append(desc_node.pk)

            # Get process label
            process_label = (
                desc_node.process_label
                if hasattr(desc_node, "process_label")
                else desc_node.node_type.split(".")[-1]
            )

            # Get state
            state = (
                desc_node.process_state if hasattr(desc_node, "process_state") else "-"
            )

            # Get exit code
            exit_code = (
                str(desc_node.exit_status)
                if hasattr(desc_node, "exit_status")
                and desc_node.exit_status is not None
                else "-"
            )

            self.table.add_row(str(desc_node.pk), process_label, state, exit_code)

        if self.title_widget is not None:
            parent_label = getattr(node, "process_label", f"Node {node.pk}")
            self.title_widget.update(
                f"[b]Called by:[/b] {parent_label} (PK: {node.pk}) | "
                f"[b]Processes:[/b] {len(self.nodes_list)}"
            )

        if self.nodes_list:
            self.table.focus()

    def show_file_list(self, node: Node) -> None:
        """Show list of available files to select from."""
        assert self.table is not None

        if not isinstance(node, orm.CalcJobNode):
            self.notify("Not a CalcJob - no files available")
            return

        retrieved_files = get_retrieved_files(node)

        if not retrieved_files:
            self.notify("No retrieved files found")
            return

        self.mode = "file_list"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Filename")

        # Show only key output files
        key_files = ["aiida.out", "_scheduler-stdout.txt", "_scheduler-stderr.txt"]
        self.available_files = [f for f in key_files if f in retrieved_files]

        for filename in self.available_files:
            self.table.add_row(filename)

        if self.title_widget is not None:
            self.title_widget.update(
                f"[b]Select file to view[/b] (PK: {node.pk}) | "
                f"Press 'a' to view file content"
            )

        if self.available_files:
            self.table.focus()

    def show_file_content(self, node: Node, filename: str) -> None:
        """Show content of selected file (last 500 lines by default)."""
        assert self.detail_view is not None

        self.mode = "file_view"
        self.table.display = False
        self.detail_view.display = True
        self.detail_view.clear()
        self.current_file = filename

        self.detail_view.write("=" * 80)
        self.detail_view.write(
            f"[bold cyan]FILE: {filename} (last {self.preview_lines} lines)[/bold cyan]"
        )
        self.detail_view.write("=" * 80)

        content = get_file_content(
            node, filename, head_lines=0, tail_lines=self.preview_lines
        )
        self.detail_view.write(content)

        if self.title_widget is not None:
            self.title_widget.update(
                f"[b]{filename}[/b] (PK: {node.pk}) | "
                f"Last {self.preview_lines} lines | Press +/- to adjust | 'b' to go back"
            )

        self.detail_view.focus()

    def action_refresh(self) -> None:
        """Reload the current view."""
        if self.mode == "groups":
            self.show_group_list()
        elif self.mode == "nodes":
            self.load_nodes()
        elif self.mode == "descendants" and self.current_node:
            self.show_descendants(self.current_node)
        elif self.mode == "file_list" and self.current_node:
            self.show_file_list(self.current_node)
        elif self.mode == "file_view" and self.current_node and self.current_file:
            self.show_file_content(self.current_node, self.current_file)

    def action_quit(self) -> None:
        """Quit the app."""
        self.exit()

    def action_select(self) -> None:
        """Handle selection in different modes."""
        assert self.table is not None

        row_index = self.table.cursor_row
        if row_index is None:
            return

        if self.mode == "groups":
            # Select group -> show nodes
            row = self.table.get_row_at(row_index)
            group_label = row[0]
            self.group_identifier = group_label
            self.load_group()
            self.setup_table()
            self.load_nodes()
            self.mode = "nodes"

        elif self.mode in ["nodes", "descendants"]:
            # Select node -> show descendants or files (if CalcJob)
            row = self.table.get_row_at(row_index)
            node_pk = int(row[0])
            self.current_node = orm.load_node(node_pk)

            # If it's a CalcJob, show file list instead of descendants
            if isinstance(self.current_node, orm.CalcJobNode):
                self.show_file_list(self.current_node)
            else:
                self.show_descendants(self.current_node)

        elif self.mode == "file_list":
            # Select file -> show content
            row = self.table.get_row_at(row_index)
            filename = row[0]
            if self.current_node:
                self.show_file_content(self.current_node, filename)

    def action_increase_preview(self) -> None:
        """Increase number of preview lines shown."""
        self.preview_lines += 50

        if self.mode == "file_view" and self.current_node and self.current_file:
            self.show_file_content(self.current_node, self.current_file)

        self.notify(f"Preview lines: {self.preview_lines}")

    def action_decrease_preview(self) -> None:
        """Decrease number of preview lines shown."""
        self.preview_lines = max(50, self.preview_lines - 50)

        if self.mode == "file_view" and self.current_node and self.current_file:
            self.show_file_content(self.current_node, self.current_file)

        self.notify(f"Preview lines: {self.preview_lines}")

    def action_go_back(self) -> None:
        """Go back one level in navigation hierarchy."""
        if self.mode == "file_view":
            # file_view -> file_list
            assert self.detail_view is not None
            assert self.table is not None

            self.detail_view.display = False
            self.table.display = True
            self.table.focus()

            if self.current_node:
                self.show_file_list(self.current_node)

        elif self.mode == "file_list":
            # file_list -> descendants
            if self.current_node:
                self.show_descendants(self.current_node)

        elif self.mode == "descendants":
            # descendants -> nodes
            self.setup_table()
            self.load_nodes()

        elif self.mode == "nodes":
            # nodes -> groups
            self.show_group_list()

        elif self.mode == "groups":
            # groups -> exit app
            self.exit()
