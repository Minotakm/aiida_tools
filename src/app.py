"""Main TUI application for browsing AiiDA groups and nodes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from aiida import orm
from aiida.common.exceptions import NotExistent
from aiida.orm import Node, load_group
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, RichLog, Static

from .node_inspector import (
    get_file_content,
    get_retrieved_files,
    get_input_files,
    get_input_file_content,
)
from .queries import (
    get_descendants,
    get_groups,
    get_nodes_in_group,
)


class TagNameScreen(ModalScreen[str]):
    """Modal screen to get tag name."""

    CSS = """
    TagNameScreen {
        align: center middle;
    }

    #dialog {
        width: 60;
        height: 11;
        border: thick $background 80%;
        background: $surface;
    }

    #question {
        height: 3;
        content-align: center middle;
    }

    Input {
        margin: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                "Enter tag name (e.g., 'memory_error', 'convergence_issue'):",
                id="question",
            ),
            Input(placeholder="tag_name", id="tag_input"),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class PatternScreen(ModalScreen[str]):
    """Modal screen to get search pattern."""

    CSS = """
    PatternScreen {
        align: center middle;
    }

    #dialog {
        width: 80;
        height: 11;
        border: thick $background 80%;
        background: $surface;
    }

    #question {
        height: 3;
        content-align: center middle;
    }

    Input {
        margin: 1 2;
    }
    """

    def __init__(self, tag_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tag_name = tag_name

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                f"Enter search pattern to find in output files for tag '{self.tag_name}':",
                id="question",
            ),
            Input(placeholder="Error message or pattern to search", id="pattern_input"),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


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
        Binding("m", "increase_preview", "More lines", show=True),
        Binding("l", "decrease_preview", "Fewer lines", show=True),
        Binding("t", "tag_error", "Tag Error", show=True),
        Binding("u", "update_tags", "Update Tags", show=True),
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
        self.root_node: Optional[Node] = None  # The workchain selected from nodes list
        self.navigation_stack: list[tuple[str, Optional[Node]]] = (
            []
        )  # Stack of (mode, node) pairs
        self.selected_group_index: int = 0  # Track selected group for back navigation
        self.selected_node_pk: Optional[int] = (
            None  # Track selected node PK for cursor restoration
        )
        self.selected_descendants: dict[int, int] = (
            {}
        )  # Map parent PK to selected child PK
        self.selected_files: dict[int, str] = {}  # Map CalcJob PK to selected filename
        self.nodes_list = []
        self.detail_view: Optional[RichLog] = None
        self.available_files = []  # List of (filename, type) tuples
        self.current_file: Optional[str] = None  # Currently selected file
        self.current_file_type: Optional[str] = None  # 'input' or 'output'

        # Settings - show last 500 lines by default for files
        self.preview_lines = 500

        # Error tagging - save in data directory at repo root
        package_dir = Path(__file__).parent
        repo_root = package_dir.parent
        data_dir = repo_root / "data"
        data_dir.mkdir(exist_ok=True)

        self.tags_file = data_dir / "tags.json"
        self.tags: dict[int, str] = {}  # Map node PK to tag name
        self.categorized_file = data_dir / "categorized.json"
        self.categorized_workchains: set[int] = (
            set()
        )  # Set of PKs that have been tagged (globally)
        self.patterns_file = data_dir / "patterns.json"
        self.error_patterns: dict[str, dict[str, str]] = (
            {}
        )  # Map tag_name -> {"filename": ..., "pattern": ...}
        self.load_tags()
        self.load_categorized()
        self.load_patterns()

        # Setup logging for debugging
        self.log_file = package_dir / ".aiida_tui_debug.log"
        logging.basicConfig(
            filename=str(self.log_file),
            level=logging.DEBUG,
            format="%(asctime)s - %(message)s",
        )
        logging.info(f"=== TUI Started - Log file: {self.log_file} ===")

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

    def load_tags(self) -> None:
        """Load tags from JSON file."""
        if self.tags_file.exists():
            try:
                with open(self.tags_file, "r") as f:
                    data = json.load(f)
                    # Check format: if first key is numeric, it's old format {pk: tag_name}
                    # New format is {tag_name: [pk1, pk2, ...]}
                    if data and next(iter(data)).isdigit():
                        # Old format - convert to new internal format
                        self.tags = {int(k): v for k, v in data.items()}
                    else:
                        # New format - convert to internal format {pk: tag_name}
                        self.tags = {}
                        for tag_name, pks in data.items():
                            for pk in pks:
                                self.tags[int(pk)] = tag_name
            except (json.JSONDecodeError, ValueError):
                self.tags = {}
        else:
            self.tags = {}

    def save_tags(self) -> None:
        """Save tags to JSON file in format {tag_name: [pk1, pk2, ...]}."""
        # Convert from internal {pk: tag_name} to {tag_name: [pks...]}
        tag_to_pks: dict[str, list[int]] = {}
        for pk, tag_name in self.tags.items():
            if tag_name not in tag_to_pks:
                tag_to_pks[tag_name] = []
            tag_to_pks[tag_name].append(pk)

        # Sort PKs within each tag
        for tag_name in tag_to_pks:
            tag_to_pks[tag_name].sort()

        with open(self.tags_file, "w") as f:
            json.dump(tag_to_pks, f, indent=2)

    def load_categorized(self) -> None:
        """Load set of categorized (tagged) workchain PKs from JSON file."""
        if self.categorized_file.exists():
            try:
                with open(self.categorized_file, "r") as f:
                    data = json.load(f)
                    self.categorized_workchains = set(int(pk) for pk in data)
            except (json.JSONDecodeError, ValueError) as e:
                logging.error(f"Error loading categorized file: {e}")
                self.categorized_workchains = set()
        else:
            self.categorized_workchains = set()

    def save_categorized(self) -> None:
        """Save set of categorized (tagged) workchain PKs to JSON file."""
        with open(self.categorized_file, "w") as f:
            data = sorted(list(self.categorized_workchains))
            json.dump(data, f, indent=2)

    def load_patterns(self) -> None:
        """Load error patterns from JSON file."""
        if self.patterns_file.exists():
            try:
                with open(self.patterns_file, "r") as f:
                    self.error_patterns = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                logging.error(f"Error loading patterns file: {e}")
                self.error_patterns = {}
        else:
            self.error_patterns = {}

    def save_patterns(self) -> None:
        """Save error patterns to JSON file."""
        with open(self.patterns_file, "w") as f:
            json.dump(self.error_patterns, f, indent=2)

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
            # Restore cursor to previously selected group
            if 0 <= self.selected_group_index < len(self.groups):
                self.table.move_cursor(row=self.selected_group_index)

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
            "PK", "UUID", "Type/Formula", "Process state", "Exit code", "Tag"
        )

    def load_nodes(self) -> None:
        """Load nodes from the group."""
        assert self.table is not None
        assert self.group is not None

        self.table.clear()
        results = get_nodes_in_group(self.group.label)

        # Sort results: failed nodes first, then by PK
        def sort_key(row):
            pk, uuid, node_type, formula, process_label, process_state, exit_status = (
                row
            )
            # Priority: excepted/killed > failed > finished with non-zero exit > finished with zero exit > others
            if process_state in ["excepted", "killed"]:
                priority = 0
            elif process_state == "finished" and exit_status and exit_status != 0:
                priority = 1
            elif process_state == "finished" and (
                exit_status is None or exit_status == 0
            ):
                priority = 3
            elif process_state:
                priority = 2  # Other states like 'waiting', 'running'
            else:
                priority = 4  # Structure data
            return (priority, pk)

        results = sorted(results, key=sort_key)
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

            tag = self.tags.get(pk, "-")
            self.table.add_row(str(pk), short_uuid, row_type, row_state, row_exit, tag)

        if self.title_widget is not None:
            self.title_widget.update(
                f"[b]Group:[/b] {self.group.label or self.group_identifier} | "
                f"[b]Nodes:[/b] {len(results)}"
            )

        if results:
            self.table.focus()
            # Restore cursor to previously selected node if it exists
            if self.selected_node_pk and self.selected_node_pk in self.nodes_list:
                row_index = self.nodes_list.index(self.selected_node_pk)
                self.table.move_cursor(row=row_index)

    def show_descendants(self, node: Node) -> None:
        """Display called WorkChains and CalcJobs only."""
        assert self.table is not None

        self.mode = "descendants"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("PK", "Process", "State", "Exit code", "Tag")

        descendants = get_descendants(node)

        # Filter to show only WorkChains and CalcJobs
        process_nodes = []
        for desc_node in descendants:
            if isinstance(desc_node, (orm.WorkChainNode, orm.CalcJobNode)):
                process_nodes.append(desc_node)

        # Sort: failed nodes first
        def sort_key(desc_node):
            state = (
                desc_node.process_state if hasattr(desc_node, "process_state") else None
            )
            exit_status = (
                desc_node.exit_status if hasattr(desc_node, "exit_status") else None
            )

            if state in ["excepted", "killed"]:
                priority = 0
            elif state == "finished" and exit_status and exit_status != 0:
                priority = 1
            elif state == "finished" and (exit_status is None or exit_status == 0):
                priority = 3
            elif state:
                priority = 2
            else:
                priority = 4
            return (priority, desc_node.pk)

        process_nodes = sorted(process_nodes, key=sort_key)
        self.nodes_list = []

        for desc_node in process_nodes:
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

            tag = self.tags.get(desc_node.pk, "-")
            self.table.add_row(str(desc_node.pk), process_label, state, exit_code, tag)

        if self.title_widget is not None:
            parent_label = getattr(node, "process_label", f"Node {node.pk}")
            self.title_widget.update(
                f"[b]Called by:[/b] {parent_label} (PK: {node.pk}) | "
                f"[b]Processes:[/b] {len(self.nodes_list)}"
            )

        if self.nodes_list:
            self.table.focus()
            # Restore cursor to previously selected child if it exists
            if node.pk in self.selected_descendants:
                selected_child_pk = self.selected_descendants[node.pk]
                if selected_child_pk in self.nodes_list:
                    row_index = self.nodes_list.index(selected_child_pk)
                    self.table.move_cursor(row=row_index)

    def show_file_list(self, node: Node) -> None:
        """Show list of available files to select from."""
        assert self.table is not None

        if not isinstance(node, orm.CalcJobNode):
            self.notify("Not a CalcJob - no files available")
            return

        retrieved_files = get_retrieved_files(node)
        input_files = get_input_files(node)

        self.mode = "file_list"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Filename", "Type")

        # Build list of available files with their types
        self.available_files = []

        # Output files from retrieved folder
        output_files = ["aiida.out", "_scheduler-stdout.txt", "_scheduler-stderr.txt"]
        for filename in output_files:
            if filename in retrieved_files:
                self.available_files.append((filename, "output"))

        # Input files from repository
        for filename in input_files:
            self.available_files.append((filename, "input"))

        if not self.available_files:
            self.notify("No files found")
            return

        for filename, file_type in self.available_files:
            self.table.add_row(filename, file_type)

        if self.title_widget is not None:
            self.title_widget.update(
                f"[b]Select file to view[/b] (PK: {node.pk}) | "
                f"Press 'a' to view file content"
            )

        if self.available_files:
            self.table.focus()
            # Restore cursor to previously selected file if it exists
            if node.pk in self.selected_files:
                selected_filename = self.selected_files[node.pk]
                # Find the file in the available files list
                for idx, (filename, _) in enumerate(self.available_files):
                    if filename == selected_filename:
                        self.table.move_cursor(row=idx)
                        break

    def show_file_content(self, node: Node, filename: str, file_type: str) -> None:
        """Show content of selected file.

        Args:
            node: The calculation node
            filename: Name of file to view
            file_type: Either 'input' or 'output'
        """
        assert self.detail_view is not None

        self.mode = "file_view"
        self.table.display = False
        self.detail_view.display = True
        self.detail_view.clear()
        self.current_file = filename
        self.current_file_type = file_type

        self.detail_view.write("=" * 80)
        if file_type == "output":
            self.detail_view.write(
                f"[bold cyan]FILE: {filename} (last {self.preview_lines} lines)[/bold cyan]"
            )
        else:
            self.detail_view.write(
                f"[bold cyan]FILE: {filename} (input file)[/bold cyan]"
            )
        self.detail_view.write("=" * 80)

        if file_type == "output":
            content = get_file_content(
                node, filename, head_lines=0, tail_lines=self.preview_lines
            )
        else:
            content = get_input_file_content(node, filename)

        self.detail_view.write(content)

        if self.title_widget is not None:
            if file_type == "output":
                self.title_widget.update(
                    f"[b]{filename}[/b] (PK: {node.pk}) | "
                    f"Last {self.preview_lines} lines | Press +/- to adjust | 'b' to go back"
                )
            else:
                self.title_widget.update(
                    f"[b]{filename}[/b] (PK: {node.pk}) | Input file | 'b' to go back"
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
        elif (
            self.mode == "file_view"
            and self.current_node
            and self.current_file
            and self.current_file_type
        ):
            self.show_file_content(
                self.current_node, self.current_file, self.current_file_type
            )

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
            self.selected_group_index = row_index  # Save selected group index
            self.group_identifier = group_label
            self.load_group()
            self.setup_table()
            self.load_nodes()
            self.mode = "nodes"

        elif self.mode in ["nodes", "descendants"]:
            # Select node -> show descendants or files (if CalcJob)
            row = self.table.get_row_at(row_index)
            node_pk = int(row[0])
            selected_node = orm.load_node(node_pk)

            # When selecting from nodes, just mark that we came from nodes (don't store the node yet)
            if self.mode == "nodes":
                self.root_node = selected_node
                self.current_node = selected_node
                self.selected_node_pk = (
                    node_pk  # Save selected node PK for cursor restoration
                )
                self.navigation_stack.append(
                    ("nodes", None)
                )  # Mark that we came from nodes list
            # When already in descendants, push current state to stack before moving
            elif self.mode == "descendants":
                self.navigation_stack.append((self.mode, self.current_node))
                # Track which child was selected for this parent
                if self.current_node:
                    self.selected_descendants[self.current_node.pk] = node_pk
                self.current_node = selected_node

            # If it's a CalcJob, show file list instead of descendants
            if isinstance(selected_node, orm.CalcJobNode):
                self.show_file_list(selected_node)
            else:
                self.show_descendants(selected_node)

        elif self.mode == "file_list":
            # Select file -> show content
            row = self.table.get_row_at(row_index)
            filename = row[0]
            file_type = row[1]  # 'input' or 'output'
            if self.current_node:
                # Track which file was selected for this CalcJob
                self.selected_files[self.current_node.pk] = filename
                self.show_file_content(self.current_node, filename, file_type)

    def action_increase_preview(self) -> None:
        """Increase number of preview lines shown (output files only)."""
        if self.current_file_type != "output":
            return

        self.preview_lines += 50

        if self.mode == "file_view" and self.current_node and self.current_file:
            self.show_file_content(
                self.current_node, self.current_file, self.current_file_type
            )

        self.notify(f"Preview lines: {self.preview_lines}")

    def action_decrease_preview(self) -> None:
        """Decrease number of preview lines shown (output files only)."""
        if self.current_file_type != "output":
            return

        self.preview_lines = max(50, self.preview_lines - 50)

        if self.mode == "file_view" and self.current_node and self.current_file:
            self.show_file_content(
                self.current_node, self.current_file, self.current_file_type
            )

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
            # Pop from navigation stack to go back to previous view
            if self.navigation_stack:
                prev_mode, prev_node = self.navigation_stack.pop()

                if prev_mode == "descendants" and prev_node:
                    self.current_node = prev_node
                    self.show_descendants(prev_node)
                elif prev_mode == "nodes":
                    # Go back to nodes list
                    self.root_node = None
                    self.mode = "nodes"
                    self.setup_table()
                    self.load_nodes()
                else:
                    # Fallback to nodes
                    self.mode = "nodes"
                    self.setup_table()
                    self.load_nodes()
            else:
                # No stack - go back to nodes
                self.mode = "nodes"
                self.setup_table()
                self.load_nodes()

        elif self.mode == "descendants":
            # Pop from navigation stack or go to nodes
            if self.navigation_stack:
                prev_mode, prev_node = self.navigation_stack.pop()

                if prev_mode == "descendants" and prev_node:
                    self.current_node = prev_node
                    self.show_descendants(prev_node)
                elif prev_mode == "nodes":
                    # We're at the root, go back to nodes list
                    self.root_node = None
                    self.mode = "nodes"
                    self.setup_table()
                    self.load_nodes()
                else:
                    # Fallback to nodes
                    self.mode = "nodes"
                    self.setup_table()
                    self.load_nodes()
            else:
                # No stack - go back to nodes
                self.mode = "nodes"
                self.setup_table()
                self.load_nodes()

        elif self.mode == "nodes":
            # nodes -> groups
            self.navigation_stack.clear()  # Clear stack when going to groups
            self.root_node = None  # Clear root node
            self.show_group_list()

        elif self.mode == "groups":
            # groups -> exit app
            self.exit()

    def action_tag_error(self) -> None:
        """Tag father workchains that contain calculations with specific errors."""
        # Only works when viewing a file (need to know which file to scan)
        if self.mode != "file_view" or not self.current_file:
            return

        if not self.group:
            return

        current_filename = self.current_file

        # Step 1: Get tag name via modal screen
        def on_tag_name_result(tag_name: str | None) -> None:
            if not tag_name or not tag_name.strip():
                return

            tag_name = tag_name.strip()

            # Step 2: Get search pattern via modal screen
            def on_pattern_result(pattern: str | None) -> None:
                if not pattern or not pattern.strip():
                    return

                pattern = pattern.strip()

                # Step 3: Scan all father workchains in group for this pattern
                self.scan_and_tag_father_workchains(tag_name, pattern, current_filename)

            self.push_screen(PatternScreen(tag_name), on_pattern_result)

        self.push_screen(TagNameScreen(), on_tag_name_result)

    def action_update_tags(self) -> None:
        """Re-scan and auto-tag workchains using previously saved error patterns."""
        if not self.group:
            if self.title_widget:
                self.title_widget.update(
                    "[b red]No group selected. Navigate to a group first.[/b red]"
                )
            return

        if not self.error_patterns:
            if self.title_widget:
                self.title_widget.update(
                    "[b yellow]No error patterns saved yet. Use 't' to create tags first.[/b yellow]"
                )
            return

        # Show progress and start scanning
        if self.title_widget:
            self.title_widget.update(
                f"[b yellow]Re-scanning with {len(self.error_patterns)} saved patterns...[/b yellow]"
            )

        total_newly_tagged = 0
        summary_parts = []

        # Scan with each saved pattern
        for tag_name, pattern_info in self.error_patterns.items():
            filename = pattern_info["filename"]
            pattern = pattern_info["pattern"]

            logging.info(
                f"Re-scanning for tag '{tag_name}' with pattern '{pattern}' in file '{filename}'"
            )

            # Reuse the existing scan method
            initial_count = len(self.tags)
            self.scan_and_tag_father_workchains(tag_name, pattern, filename)
            newly_tagged = len(self.tags) - initial_count
            total_newly_tagged += newly_tagged

            if newly_tagged > 0:
                summary_parts.append(f"{newly_tagged} '{tag_name}'")

        # Show final summary
        if self.title_widget:
            if total_newly_tagged > 0:
                summary = ", ".join(summary_parts)
                self.title_widget.update(
                    f"[b green]Re-scan complete! Newly tagged: {summary}[/b green]"
                )
            else:
                self.title_widget.update(
                    f"[b green]Re-scan complete! No new workchains matched the patterns.[/b green]"
                )

    def scan_and_tag_father_workchains(
        self, tag_name: str, pattern: str, filename: str
    ) -> None:
        """Scan all father workchains in group and tag those with the error pattern."""
        tagged_count = 0
        total_failed_calcs = 0

        qb = orm.QueryBuilder()
        qb.append(orm.Group, filters={"label": self.group.label}, tag="group")
        qb.append(
            orm.WorkChainNode,
            with_group="group",
            filters={
                "and": [
                    {"attributes.exit_status": {"!==": 0}},
                    {"attributes.process_state": "finished"},
                ]
            },
            project=["*"],
        )

        failed_workchains = qb.all(flat=True)
        scanned_count = len(failed_workchains)

        # Filter out already categorized (tagged) workchains globally
        uncategorized_workchains = [
            wc for wc in failed_workchains if wc.pk not in self.categorized_workchains
        ]
        skipped_count = scanned_count - len(uncategorized_workchains)

        logging.info(
            f"Tag '{tag_name}': Total failed workchains: {scanned_count}, Already tagged (globally): {skipped_count}, To scan: {len(uncategorized_workchains)}"
        )

        for idx, workchain in enumerate(uncategorized_workchains, 1):
            try:
                # Update progress every 10 workchains
                if idx % 10 == 0 and self.title_widget:
                    self.title_widget.update(
                        f"[b yellow]Scanning... {idx}/{len(uncategorized_workchains)} workchains (skipped {skipped_count})[/b yellow]"
                    )

                # Check if this workchain has the error pattern
                has_error, num_failed = self.workchain_has_error_fast(
                    workchain, pattern, filename
                )
                total_failed_calcs += num_failed

                if has_error:
                    self.tags[workchain.pk] = tag_name
                    tagged_count += 1

                    # Mark as categorized globally (will be skipped in all future scans)
                    self.categorized_workchains.add(workchain.pk)
                    logging.info(
                        f"Tagged workchain {workchain.pk} with '{tag_name}' and marked as categorized"
                    )

                    # Debug: show which workchain was tagged
                    if self.title_widget:
                        self.title_widget.update(
                            f"[b green]Tagged workchain {workchain.pk}[/b green]"
                        )
                else:
                    logging.debug(
                        f"Workchain {workchain.pk} - pattern not found, will check in next scan"
                    )

            except Exception as e:
                # Don't mark as categorized if there was an error - allow retry in next scan
                logging.error(
                    f"Error on workchain {workchain.pk}: {str(e)} - will retry in next scan"
                )
                # Debug: show if there's an exception
                if self.title_widget:
                    self.title_widget.update(
                        f"[b red]Error on workchain {workchain.pk}: {str(e)}[/b red]"
                    )
                continue

        # Save pattern for this tag for future re-scans
        self.error_patterns[tag_name] = {"filename": filename, "pattern": pattern}
        self.save_patterns()

        # Save tags and categorized workchains to file
        self.save_tags()
        self.save_categorized()

        # Go back to nodes view to show updated tags
        while self.mode != "nodes":
            self.action_go_back()

        # Refresh nodes view
        self.load_nodes()

        # Show notification
        if self.title_widget:
            self.title_widget.update(
                f"[b green]Scanned {len(uncategorized_workchains)} workchains (skipped {skipped_count}), {total_failed_calcs} calcs checked, tagged {tagged_count} with '{tag_name}'[/b green]"
            )

    def workchain_has_error_fast(
        self, workchain: orm.WorkChainNode, pattern: str, filename: str
    ) -> tuple[bool, int]:
        """Check if workchain has any failed CalcJob with the pattern (using QueryBuilder).
        Returns (has_error, num_failed_calcs_checked).
        """
        try:
            # OPTIMIZED: Query following the hierarchy: father -> failed child workchains -> last CalcJob
            qb = orm.QueryBuilder()
            qb.append(
                orm.WorkChainNode,
                filters={"id": workchain.pk},
                tag="father",
            )
            qb.append(
                orm.WorkChainNode,
                with_incoming="father",
                filters={"attributes.exit_status": {"!==": 0}},
                tag="child_wc",
            )
            qb.append(
                orm.CalcJobNode,
                with_incoming="child_wc",
                project=["*", "ctime"],
                tag="calcjob",
            )

            # Order by CalcJob creation time descending to get the most recent first
            qb.order_by({"calcjob": {"ctime": "desc"}})
            qb.limit(1)

            result = qb.all()

            if not result:
                logging.debug(f"WC {workchain.pk}: No failed CalcJob found")
                return False, 1  # Still count as 1 attempt even if no CalcJob found

            # Get the most recent CalcJob
            calc_node = result[0][0]
            logging.debug(
                f"WC {workchain.pk}: Checking CalcJob {calc_node.pk} for pattern '{pattern}' in file '{filename}'"
            )

            # Check pattern in the last CalcJob
            found = self.search_pattern_in_file(calc_node, pattern, filename)
            logging.debug(
                f"WC {workchain.pk}: CalcJob {calc_node.pk} - Pattern found: {found}"
            )

            if found:
                return True, 1

            return False, 1
        except Exception as e:
            # Log error but don't crash
            return False, 0

    def search_pattern_in_file(
        self, node: orm.CalcJobNode, pattern: str, filename: str
    ) -> bool:
        """Search for pattern in a specific file of the node."""
        try:
            pattern_lower = pattern.lower()

            # Check if it's an output file
            retrieved_files = get_retrieved_files(node)
            logging.debug(f"CalcJob {node.pk}: Retrieved files: {retrieved_files}")

            if filename in retrieved_files:
                content = get_file_content(
                    node, filename, head_lines=0, tail_lines=2000
                )
                found = pattern_lower in content.lower()
                logging.debug(
                    f"CalcJob {node.pk}: Checked output '{filename}', found={found}, content length={len(content)}"
                )
                return found

            # Check if it's an input file
            input_files = get_input_files(node)
            logging.debug(f"CalcJob {node.pk}: Input files: {input_files}")

            if filename in input_files:
                content = get_input_file_content(node, filename)
                found = pattern_lower in content.lower()
                logging.debug(
                    f"CalcJob {node.pk}: Checked input '{filename}', found={found}, content length={len(content)}"
                )
                return found

            # File not found
            logging.warning(
                f"CalcJob {node.pk}: File '{filename}' not found! Available: {retrieved_files + input_files}"
            )
            return False
        except Exception as e:
            logging.error(f"CalcJob {node.pk}: Exception: {str(e)}")
            return False
