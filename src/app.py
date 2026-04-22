"""Main TUI application for browsing AiiDA groups and nodes."""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from aiida import orm
from aiida.common.exceptions import NotExistent
from aiida.orm import Node, load_group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, Static, TextArea
from textual import work

from .node_inspector import (
    get_file_content,
    get_full_file_content,
    get_retrieved_files,
    get_retrieved_file_size,
    get_input_files,
    get_input_file_content,
    get_input_file_size,
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


class TagInspectorScreen(ModalScreen):
    """Read-only modal listing every tag, its count, and the pattern/file used."""

    CSS = """
    TagInspectorScreen {
        align: center middle;
    }

    #dialog {
        width: 100;
        height: 30;
        border: thick $background 80%;
        background: $surface;
    }

    #inspector_title {
        height: 1;
        content-align: center middle;
    }

    #inspector_hint {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }

    DataTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        tags: dict[int, str],
        patterns: dict[str, dict[str, str]],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._tags = tags
        self._patterns = patterns

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("[b]Tag Inspector[/b]", id="inspector_title"),
            Label("Press Escape to close", id="inspector_hint"),
            DataTable(zebra_stripes=True),
            id="dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Tag", "Count", "File", "Pattern")

        counts: dict[str, int] = {}
        for tag_name in self._tags.values():
            counts[tag_name] = counts.get(tag_name, 0) + 1

        # Include tags with 0 matches if a pattern exists but no PKs are tagged yet.
        all_tag_names = set(counts) | set(self._patterns)
        rows = []
        for tag_name in sorted(all_tag_names):
            info = self._patterns.get(tag_name, {})
            rows.append(
                (
                    tag_name,
                    str(counts.get(tag_name, 0)),
                    info.get("filename", "-"),
                    info.get("pattern", "-"),
                )
            )
        if rows:
            table.add_rows(rows)
        table.focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(None)


class FileSearchScreen(ModalScreen[tuple[str, int] | None]):
    """Prompt for a pattern and context window when searching inside a file."""

    CSS = """
    FileSearchScreen {
        align: center middle;
    }

    #dialog {
        width: 80;
        height: 13;
        border: thick $background 80%;
        background: $surface;
    }

    #hint {
        height: 2;
        content-align: center middle;
        color: $text-muted;
    }

    Input {
        margin: 1 2;
    }
    """

    def __init__(self, initial_pattern: str = "", initial_context: int = 5, **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial_pattern = initial_pattern
        self._initial_context = initial_context

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Search pattern (case-insensitive):", id="hint"),
            Input(value=self._initial_pattern, placeholder="e.g. total magnetization", id="pattern_input"),
            Input(value=str(self._initial_context), placeholder="context lines after match", id="context_input"),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#pattern_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        pattern = self.query_one("#pattern_input", Input).value.strip()
        raw_ctx = self.query_one("#context_input", Input).value.strip()
        try:
            context = max(0, int(raw_ctx)) if raw_ctx else 0
        except ValueError:
            context = 0
        if not pattern:
            self.dismiss(None)
            return
        self.dismiss((pattern, context))

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(None)


class PresetScreen(ModalScreen[dict | None]):
    """Pick a saved QE (or user-defined) search preset."""

    CSS = """
    PresetScreen {
        align: center middle;
    }

    #dialog {
        width: 90;
        height: 24;
        border: thick $background 80%;
        background: $surface;
    }

    #preset_title {
        height: 1;
        content-align: center middle;
    }

    #preset_hint {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }

    DataTable {
        height: 1fr;
    }
    """

    def __init__(self, presets: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._presets = presets

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("[b]Search presets[/b]", id="preset_title"),
            Label("Enter to apply · Escape to cancel", id="preset_hint"),
            DataTable(zebra_stripes=True),
            id="dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Pattern", "Context")
        for p in self._presets:
            table.add_row(
                p.get("name", ""),
                p.get("pattern", ""),
                str(p.get("context", 0)),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self._presets):
            self.dismiss(self._presets[idx])
        else:
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            self.dismiss(None)


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

        TextArea {
            height: 1fr;
            border: solid green;
            background: $surface;
        }

        #search_input {
            visibility: hidden;
            dock: bottom;
            height: 3;
            margin: 0 1;
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
        Binding("T", "filter_by_tag", "Filter tagged", show=True),
        Binding("u", "update_tags", "Update Tags", show=True),
        Binding("x", "untag", "Untag", show=True),
        Binding("i", "tag_inspector", "Tag Inspector", show=True),
        Binding("e", "export_tagged", "Export", show=True),
        Binding("slash", "search", "Search", show=True),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "prev_match", "Prev match", show=False),
        Binding("L", "last_match", "Last match", show=False),
        Binding("F", "toggle_filter", "Filter matches", show=False),
        Binding("p", "presets", "Presets", show=False),
        Binding("f", "open_pager", "Open in pager", show=False),
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
        self.detail_view: Optional[TextArea] = None
        self.available_files = []  # List of (filename, type) tuples
        self.current_file: Optional[str] = None  # Currently selected file
        self.current_file_type: Optional[str] = None  # 'input' or 'output'

        # Settings - show last 500 lines by default for files
        self.preview_lines = 500

        # Search/filter state
        self._all_table_rows: list[tuple] = []  # Unfiltered rows for current table view
        self._all_table_rows_lower: list[str] = []  # Pre-computed lowercase joins for fast filtering
        self._search_active = False
        self._search_debounce_timer = None
        self._base_title = ""  # Title without search match-count suffix
        self._tag_filter = "all"  # "all", "tagged", "untagged" — cycled via T

        # Scanning state
        self._scanning = False

        # Error tagging - save in data directory at repo root
        package_dir = Path(__file__).parent
        repo_root = package_dir.parent
        data_dir = repo_root / "data"
        data_dir.mkdir(exist_ok=True)

        # In-file search state (file_view mode)
        self._file_full_lines: list[str] = []
        self._file_content_cache: dict[tuple[int, str, str], str] = {}
        self._search_pattern: str = ""
        self._search_context: int = 5
        self._search_matches: list[int] = []  # source line indices of matches
        self._search_current: int = -1
        self._filter_mode: bool = False
        self._display_to_source: list[int] = []  # in filter mode, display-line -> source-line
        self._source_to_display: dict[int, int] = {}
        self._file_header_line_count: int = 0  # how many header lines prepend the content

        self.presets_file = data_dir / "qe_patterns.json"
        self.search_presets: list[dict] = []
        self._load_or_seed_presets()

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
        self.settings_file = data_dir / "settings.json"
        self.load_tags()
        self.load_categorized()
        self.load_patterns()
        self.load_settings()

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
            self.detail_view = TextArea(read_only=True)
            self.detail_view.display = False
            yield self.detail_view
        yield Input(placeholder="Search... (Escape to close)", id="search_input")
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

    def load_settings(self) -> None:
        """Load persisted user settings (e.g. preview_lines)."""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, "r") as f:
                    data = json.load(f)
                value = data.get("preview_lines")
                if isinstance(value, int) and value > 0:
                    self.preview_lines = value
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logging.error(f"Error loading settings file: {e}")

    def save_settings(self) -> None:
        """Persist user settings."""
        settings = {"preview_lines": self.preview_lines}
        with open(self.settings_file, "w") as f:
            json.dump(settings, f, indent=2)

    def _set_title(self, text: str) -> None:
        """Set title widget and remember the base (non-search) title."""
        self._base_title = text
        if self.title_widget is not None:
            self.title_widget.update(text)

    def _format_node_breadcrumb(self, node: Node) -> str:
        """Render a node as a compact breadcrumb segment."""
        if isinstance(node, orm.WorkChainNode):
            return f"WC {node.pk}"
        if isinstance(node, orm.CalcJobNode):
            return f"CalcJob {node.pk}"
        return f"Node {node.pk}"

    def _render_breadcrumb(self) -> str:
        """Build a 'Groups › group › WC 1234 › CalcJob 5678 › file' breadcrumb."""
        parts = ["[b]Groups[/b]"]
        if self.mode == "groups":
            return " › ".join(parts)

        if self.group is not None:
            parts.append(f"[b]{self.group.label}[/b]")

        if self.mode == "nodes":
            return " › ".join(parts)

        # descendants / file_list / file_view: append the node path.
        node_path: list[Node] = []
        for _, stack_node in self.navigation_stack:
            if stack_node is not None:
                node_path.append(stack_node)
        if self.current_node is not None:
            if not node_path or node_path[-1].pk != self.current_node.pk:
                node_path.append(self.current_node)

        for n in node_path:
            parts.append(self._format_node_breadcrumb(n))

        if self.mode == "file_view" and self.current_file:
            parts.append(f"[b]{self.current_file}[/b]")

        return " › ".join(parts)

    def _set_breadcrumb_title(self, suffix: str = "") -> None:
        """Set title to breadcrumb + optional suffix."""
        breadcrumb = self._render_breadcrumb()
        if suffix:
            self._set_title(f"{breadcrumb} | {suffix}")
        else:
            self._set_title(breadcrumb)

    @staticmethod
    def _format_size(nbytes: int | None) -> str:
        """Human-readable file size."""
        if nbytes is None:
            return "?"
        size = float(nbytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(size)} B"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    def _set_table_rows(self, rows: list[tuple]) -> None:
        """Cache rows + lowercase strings, and bulk-load into the table.

        Assumes columns are already configured. Replaces any existing rows.
        """
        assert self.table is not None
        self._all_table_rows = rows
        self._all_table_rows_lower = [
            " ".join(str(cell).lower() for cell in row) for row in rows
        ]
        with self.batch_update():
            self.table.clear()
            if rows:
                self.table.add_rows(rows)

    def _dismiss_search(self) -> None:
        """Close search bar if open."""
        if self._search_active:
            self._search_active = False
            try:
                search_input = self.query_one("#search_input", Input)
                search_input.visible = False
                search_input.value = ""
            except Exception:
                pass

    def show_group_list(self) -> None:
        """Populate the table with available groups."""
        assert self.table is not None
        self._dismiss_search()

        self.mode = "groups"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Label", "Type", "#Nodes")

        self.groups = get_groups()
        rows = [
            (g["label"], g["type_string"], str(g["n_nodes"])) for g in self.groups
        ]
        self._set_table_rows(rows)

        self._set_breadcrumb_title("Select a group to analyse")

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
        self._dismiss_search()
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns(
            "PK", "UUID", "Type/Formula", "Process state", "Exit code", "Tag"
        )

    def load_nodes(self) -> None:
        """Load nodes from the group."""
        assert self.table is not None
        assert self.group is not None

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

        rows = []
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
            tag_cell = Text(tag, style="bold yellow") if tag != "-" else tag
            rows.append((str(pk), short_uuid, row_type, row_state, row_exit, tag_cell))

        self._set_table_rows(rows)

        self._set_breadcrumb_title(f"[b]Nodes:[/b] {len(results)}")
        if self._tag_filter != "all":
            self._apply_search_filter("")

        if results:
            self.table.focus()
            # Restore cursor to previously selected node if it exists
            if self.selected_node_pk and self.selected_node_pk in self.nodes_list:
                row_index = self.nodes_list.index(self.selected_node_pk)
                self.table.move_cursor(row=row_index)

    def show_descendants(self, node: Node) -> None:
        """Display called WorkChains and CalcJobs only."""
        assert self.table is not None
        self._dismiss_search()

        self.mode = "descendants"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("PK", "Process", "State", "Exit code", "Tag")

        process_nodes = get_descendants(node)

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
        rows = []

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
            tag_cell = Text(tag, style="bold yellow") if tag != "-" else tag
            rows.append((str(desc_node.pk), process_label, state, exit_code, tag_cell))

        self._set_table_rows(rows)

        self._set_breadcrumb_title(f"[b]Processes:[/b] {len(self.nodes_list)}")
        if self._tag_filter != "all":
            self._apply_search_filter("")

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
        self._dismiss_search()

        if not isinstance(node, orm.CalcJobNode):
            self.notify("Not a CalcJob - no files available")
            return

        retrieved_files = get_retrieved_files(node)
        input_files = get_input_files(node)

        self.mode = "file_list"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Filename", "Type", "Size")

        # Build list of available files with their types
        self.available_files = []
        rows: list[tuple] = []

        # Output files from retrieved folder
        output_files = ["aiida.out", "_scheduler-stdout.txt", "_scheduler-stderr.txt"]
        for filename in output_files:
            if filename in retrieved_files:
                self.available_files.append((filename, "output"))
                size = get_retrieved_file_size(node, filename)
                rows.append((filename, "output", self._format_size(size)))

        # Input files from repository
        for filename in input_files:
            self.available_files.append((filename, "input"))
            size = get_input_file_size(node, filename)
            rows.append((filename, "input", self._format_size(size)))

        if not self.available_files:
            self.notify("No files found")
            return

        self._set_table_rows(rows)

        self._set_breadcrumb_title("Select file to view (press 'a')")

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
        self.current_file = filename
        self.current_file_type = file_type

        self._reset_search_state()

        # Build header
        header = "=" * 80 + "\n"
        if file_type == "output":
            header += f"FILE: {filename} (last {self.preview_lines} lines)\n"
        else:
            header += f"FILE: {filename} (input file)\n"
        header += "=" * 80 + "\n"
        self._file_header_line_count = header.count("\n")

        if file_type == "output":
            content = get_file_content(
                node, filename, head_lines=0, tail_lines=self.preview_lines
            )
        else:
            content = get_input_file_content(node, filename)

        self.detail_view.text = header + content

        if file_type == "output":
            self._set_breadcrumb_title(
                f"Last {self.preview_lines} lines | m/l to adjust | '/' search | 'b' back"
            )
        else:
            self._set_breadcrumb_title("Input file | '/' search | 'b' to go back")

        self.detail_view.focus()

    def _reset_search_state(self) -> None:
        """Clear per-file search/filter state."""
        self._search_pattern = ""
        self._search_matches = []
        self._search_current = -1
        self._filter_mode = False
        self._display_to_source = []
        self._source_to_display = {}
        self._file_full_lines = []

    def _load_or_seed_presets(self) -> None:
        """Load QE search presets, seeding a default file if missing."""
        default_presets = [
            {"name": "JOB DONE", "pattern": "JOB DONE", "context": 3},
            {"name": "total magnetization", "pattern": "total magnetization", "context": 2},
            {"name": "Forces acting on atoms", "pattern": "Forces acting on atoms", "context": 20},
            {"name": "total energy (!)", "pattern": "!    total energy", "context": 1},
            {"name": "convergence achieved", "pattern": "convergence has been achieved", "context": 3},
            {"name": "error block (%%%%)", "pattern": "%%%%", "context": 10},
            {"name": "CRASH", "pattern": "CRASH", "context": 20},
            {"name": "SCF iteration", "pattern": "iteration #", "context": 1},
        ]
        if not self.presets_file.exists():
            try:
                with open(self.presets_file, "w") as f:
                    json.dump({"presets": default_presets}, f, indent=2)
                self.search_presets = default_presets
                return
            except OSError as e:
                logging.error(f"Could not seed presets file: {e}")
                self.search_presets = default_presets
                return
        try:
            with open(self.presets_file, "r") as f:
                data = json.load(f)
            presets = data.get("presets", []) if isinstance(data, dict) else []
            self.search_presets = [p for p in presets if isinstance(p, dict) and p.get("pattern")]
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logging.error(f"Error loading presets: {e}")
            self.search_presets = default_presets

    def _load_full_file_content(self) -> str:
        """Return the full text of the currently viewed file (cached)."""
        if not self.current_node or not self.current_file or not self.current_file_type:
            return ""
        key = (self.current_node.pk, self.current_file, self.current_file_type)
        if key in self._file_content_cache:
            return self._file_content_cache[key]
        content = get_full_file_content(
            self.current_node, self.current_file, self.current_file_type
        )
        self._file_content_cache[key] = content
        return content

    def _run_file_search(self, pattern: str, context: int) -> None:
        """Search the full file for a pattern and render results."""
        if self.mode != "file_view" or not self.current_file:
            return

        content = self._load_full_file_content()
        if content.startswith("[Error") or content.startswith("[No "):
            self.notify(content, severity="error")
            return

        lines = content.splitlines()
        self._file_full_lines = lines
        self._search_pattern = pattern
        self._search_context = context

        pat_lower = pattern.lower()
        self._search_matches = [
            i for i, line in enumerate(lines) if pat_lower in line.lower()
        ]

        if not self._search_matches:
            self._search_current = -1
            self.notify(f"No matches for '{pattern}'")
            self._render_file_view()
            self._update_search_title()
            return

        self._search_current = 0
        self._render_file_view()
        self._jump_to_current_match()

    def _render_file_view(self) -> None:
        """Rebuild detail_view.text from current state (scroll or filter mode)."""
        assert self.detail_view is not None
        if self._filter_mode and self._search_matches:
            self._render_filter_view()
        else:
            self._render_scroll_view()

    def _render_scroll_view(self) -> None:
        """Render the full file content with a simple header."""
        assert self.detail_view is not None
        lines = self._file_full_lines
        header = "=" * 80 + "\n"
        header += f"FILE: {self.current_file} (full file, {len(lines)} lines)\n"
        if self._search_pattern:
            header += (
                f"SEARCH: '{self._search_pattern}'  matches: {len(self._search_matches)}\n"
            )
        header += "=" * 80 + "\n"
        self._file_header_line_count = header.count("\n")
        self._display_to_source = []
        self._source_to_display = {}
        for i in range(len(lines)):
            display_line = self._file_header_line_count + i
            self._display_to_source.append(i)
            self._source_to_display[i] = display_line
        self.detail_view.text = header + "\n".join(lines)

    def _render_filter_view(self) -> None:
        """Render only match lines with ±context lines, separated by '---' markers."""
        assert self.detail_view is not None
        lines = self._file_full_lines
        ctx = self._search_context
        # Merge overlapping match windows into blocks.
        blocks: list[tuple[int, int]] = []
        for m in self._search_matches:
            start = max(0, m - ctx)
            end = min(len(lines) - 1, m + ctx)
            if blocks and start <= blocks[-1][1] + 1:
                blocks[-1] = (blocks[-1][0], max(blocks[-1][1], end))
            else:
                blocks.append((start, end))

        header = "=" * 80 + "\n"
        header += (
            f"FILTER: '{self._search_pattern}'  matches: {len(self._search_matches)}  "
            f"context: ±{ctx}\n"
        )
        header += "Press F to return to full-file view.\n"
        header += "=" * 80 + "\n"
        self._file_header_line_count = header.count("\n")

        out_lines: list[str] = []
        self._display_to_source = []
        self._source_to_display = {}
        current_display = self._file_header_line_count

        for bi, (s, e) in enumerate(blocks):
            if bi > 0:
                out_lines.append(f"--- [lines {s + 1}-{e + 1}] ---")
                self._display_to_source.append(-1)
                current_display += 1
            for src in range(s, e + 1):
                out_lines.append(f"{src + 1:>6}: {lines[src]}")
                self._display_to_source.append(src)
                self._source_to_display[src] = current_display
                current_display += 1

        self.detail_view.text = header + "\n".join(out_lines)

    def _jump_to_current_match(self) -> None:
        """Scroll to and highlight the current match."""
        assert self.detail_view is not None
        if not self._search_matches or self._search_current < 0:
            return
        src_line = self._search_matches[self._search_current]
        disp_line = self._source_to_display.get(src_line)
        if disp_line is None:
            # Re-render if match isn't in current display (shouldn't happen, guard anyway)
            self._render_file_view()
            disp_line = self._source_to_display.get(src_line)
            if disp_line is None:
                return
        pat_len = len(self._search_pattern)
        # Find the column where the pattern starts on this line (case-insensitive)
        displayed_text = ""
        try:
            displayed_text = self.detail_view.document.get_line(disp_line)
        except Exception:
            displayed_text = ""
        col = displayed_text.lower().find(self._search_pattern.lower())
        if col < 0:
            col = 0
        try:
            self.detail_view.selection = (
                (disp_line, col),
                (disp_line, col + pat_len),
            )
        except Exception:
            try:
                self.detail_view.cursor_location = (disp_line, col)
            except Exception:
                pass
        self._update_search_title()

    def _update_search_title(self) -> None:
        """Reflect current match counter in the title."""
        if not self._search_pattern:
            return
        n = len(self._search_matches)
        if n == 0:
            suffix = f"'{self._search_pattern}' — no matches"
        else:
            mode = "filter" if self._filter_mode else "scroll"
            suffix = (
                f"'{self._search_pattern}' — {self._search_current + 1}/{n} "
                f"({mode}) | n/N next/prev · L last · F filter · Esc clear"
            )
        self._set_breadcrumb_title(suffix)

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
        self.save_settings()

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
        self.save_settings()

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
            self.notify("No group selected. Navigate to a group first.", severity="error")
            return

        if not self.error_patterns:
            self.notify("No error patterns saved yet. Use 't' to create tags first.", severity="warning")
            return

        if self._scanning:
            self.notify("A scan is already in progress.", severity="warning")
            return

        self._run_update_tags(self.group.label)

    @work(thread=True)
    def _run_update_tags(self, group_label: str) -> None:
        """Background worker to re-scan with all saved patterns."""
        self._scanning = True
        try:
            total_newly_tagged = 0
            summary_parts = []

            for tag_name, pattern_info in self.error_patterns.items():
                filename = pattern_info["filename"]
                pattern = pattern_info["pattern"]
                logging.info(
                    f"Re-scanning for tag '{tag_name}' with pattern '{pattern}' in file '{filename}'"
                )
                initial_count = len(self.tags)
                self._scan_workchains(tag_name, pattern, filename, group_label)
                newly_tagged = len(self.tags) - initial_count
                total_newly_tagged += newly_tagged
                if newly_tagged > 0:
                    summary_parts.append(f"{newly_tagged} '{tag_name}'")

            if total_newly_tagged > 0:
                summary = ", ".join(summary_parts)
                msg = f"[b green]Re-scan complete! Newly tagged: {summary}[/b green]"
            else:
                msg = "[b green]Re-scan complete! No new workchains matched.[/b green]"
            self.call_from_thread(self._finish_scan, msg)
        finally:
            self._scanning = False

    def scan_and_tag_father_workchains(
        self, tag_name: str, pattern: str, filename: str
    ) -> None:
        """Launch background scan for error pattern tagging."""
        if self._scanning:
            self.notify("A scan is already in progress.", severity="warning")
            return
        if not self.group:
            return
        self._run_scan_worker(tag_name, pattern, filename, self.group.label)

    @work(thread=True)
    def _run_scan_worker(
        self, tag_name: str, pattern: str, filename: str, group_label: str
    ) -> None:
        """Background worker for a single tag scan."""
        self._scanning = True
        try:
            self._scan_workchains(tag_name, pattern, filename, group_label)

            # Save pattern for this tag for future re-scans
            self.error_patterns[tag_name] = {"filename": filename, "pattern": pattern}
            self.save_patterns()
            self.save_tags()
            self.save_categorized()

            self.call_from_thread(self._finish_scan_and_navigate, tag_name)
        finally:
            self._scanning = False

    def _finish_scan(self, message: str) -> None:
        """Called on main thread after scan completes to update UI."""
        if self.mode == "nodes":
            self.load_nodes()
        self._set_title(message)

    def _finish_scan_and_navigate(self, tag_name: str) -> None:
        """Called on main thread after single-tag scan to navigate back and refresh."""
        # Navigate back to nodes view
        while self.mode != "nodes":
            self.action_go_back()
        self.load_nodes()
        tagged_with_tag = sum(1 for t in self.tags.values() if t == tag_name)
        self._set_title(
            f"[b green]Scan complete! {tagged_with_tag} workchains tagged with '{tag_name}'[/b green]"
        )

    def _scan_workchains(
        self, tag_name: str, pattern: str, filename: str, group_label: str
    ) -> None:
        """Core scanning logic (runs in background thread)."""
        tagged_count = 0
        total_failed_calcs = 0

        qb = orm.QueryBuilder()
        qb.append(orm.Group, filters={"label": group_label}, tag="group")
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
                    self.call_from_thread(
                        self._set_title,
                        f"[b yellow]Scanning '{tag_name}'... {idx}/{len(uncategorized_workchains)} (skipped {skipped_count})[/b yellow]",
                    )

                # Check if this workchain has the error pattern
                has_error, num_failed = self.workchain_has_error_fast(
                    workchain, pattern, filename
                )
                total_failed_calcs += num_failed

                if has_error:
                    self.tags[workchain.pk] = tag_name
                    tagged_count += 1
                    self.categorized_workchains.add(workchain.pk)
                    logging.info(
                        f"Tagged workchain {workchain.pk} with '{tag_name}' and marked as categorized"
                    )
                else:
                    logging.debug(
                        f"Workchain {workchain.pk} - pattern not found, will check in next scan"
                    )

            except Exception as e:
                logging.error(
                    f"Error on workchain {workchain.pk}: {str(e)} - will retry in next scan"
                )
                continue

        logging.info(
            f"Scan complete for '{tag_name}': scanned {len(uncategorized_workchains)}, tagged {tagged_count}"
        )

    def action_filter_by_tag(self) -> None:
        """Cycle the tag-only filter: all → tagged → untagged → all."""
        if self.mode not in ("nodes", "descendants"):
            self.notify("Tag filter only available in node lists")
            return

        cycle = {"all": "tagged", "tagged": "untagged", "untagged": "all"}
        self._tag_filter = cycle[self._tag_filter]
        label = {
            "all": "Showing all rows",
            "tagged": "Showing tagged only",
            "untagged": "Showing untagged only",
        }[self._tag_filter]
        self.notify(label)

        current_query = ""
        try:
            search_input = self.query_one("#search_input", Input)
            current_query = search_input.value
        except Exception:
            pass
        self._apply_search_filter(current_query)

    def action_untag(self) -> None:
        """Remove the tag from the row under the cursor."""
        if self.mode not in ("nodes", "descendants"):
            return
        if self.table is None or self.table.cursor_row is None:
            return
        try:
            row = self.table.get_row_at(self.table.cursor_row)
            pk = int(row[0])
        except (ValueError, IndexError):
            return
        if pk not in self.tags:
            self.notify("Row is not tagged", severity="warning")
            return

        tag_name = self.tags.pop(pk)
        self.categorized_workchains.discard(pk)
        self.save_tags()
        self.save_categorized()
        self.notify(f"Removed tag '{tag_name}' from PK {pk}")

        if self.mode == "nodes":
            self.load_nodes()
        elif self.mode == "descendants" and self.current_node:
            self.show_descendants(self.current_node)

    def action_tag_inspector(self) -> None:
        """Open a read-only modal listing all tags, counts, and patterns."""
        self.push_screen(TagInspectorScreen(dict(self.tags), dict(self.error_patterns)))

    def action_export_tagged(self) -> None:
        """Write currently-displayed tagged PKs to data/export_<timestamp>.txt."""
        if self.mode not in ("nodes", "descendants"):
            self.notify("Export only works from node lists", severity="warning")
            return

        tagged_pks = [pk for pk in self.nodes_list if pk in self.tags]
        if not tagged_pks:
            self.notify("No tagged workchains in current view", severity="warning")
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = self.tags_file.parent / f"export_{timestamp}.txt"

        by_tag: dict[str, list[int]] = {}
        for pk in tagged_pks:
            by_tag.setdefault(self.tags[pk], []).append(pk)

        lines = [
            f"# Exported {datetime.datetime.now().isoformat(timespec='seconds')}",
        ]
        if self.group is not None:
            lines.append(f"# Group: {self.group.label}")
        lines.append(f"# Mode: {self.mode}")
        lines.append(f"# Tag filter: {self._tag_filter}")
        lines.append(f"# Total tagged PKs: {len(tagged_pks)}")
        lines.append("")
        for tag_name in sorted(by_tag):
            pks = sorted(by_tag[tag_name])
            lines.append(f"[{tag_name}] ({len(pks)})")
            lines.extend(str(pk) for pk in pks)
            lines.append("")

        export_path.write_text("\n".join(lines))
        self.notify(f"Exported {len(tagged_pks)} tagged PKs → {export_path}")

    def action_search(self) -> None:
        """Toggle search/filter bar for table views, or open file search in file_view."""
        if self.mode == "file_view":
            self._open_file_search_prompt()
            return

        if self.mode not in ("groups", "nodes", "descendants", "file_list"):
            return

        search_input = self.query_one("#search_input", Input)
        if self._search_active:
            # Close search, restore full table
            self._search_active = False
            search_input.visible = False
            search_input.value = ""
            self._apply_search_filter("")
            self.table.focus()
        else:
            self._search_active = True
            search_input.visible = True
            search_input.value = ""
            search_input.focus()

    def _open_file_search_prompt(self) -> None:
        """Push the FileSearchScreen modal to collect pattern + context."""
        def on_result(result: tuple[str, int] | None) -> None:
            if not result:
                return
            pattern, context = result
            self._run_file_search(pattern, context)

        self.push_screen(
            FileSearchScreen(
                initial_pattern=self._search_pattern,
                initial_context=self._search_context,
            ),
            on_result,
        )

    def action_next_match(self) -> None:
        """Jump to next match in the current file view."""
        if self.mode != "file_view" or not self._search_matches:
            return
        self._search_current = (self._search_current + 1) % len(self._search_matches)
        self._jump_to_current_match()

    def action_prev_match(self) -> None:
        """Jump to previous match in the current file view."""
        if self.mode != "file_view" or not self._search_matches:
            return
        self._search_current = (self._search_current - 1) % len(self._search_matches)
        self._jump_to_current_match()

    def action_last_match(self) -> None:
        """Jump to the last match — useful for QE outputs where later iterations matter."""
        if self.mode != "file_view" or not self._search_matches:
            return
        self._search_current = len(self._search_matches) - 1
        self._jump_to_current_match()

    def action_toggle_filter(self) -> None:
        """Switch between scroll view (full file) and filter view (matches + context)."""
        if self.mode != "file_view":
            return
        if not self._search_matches:
            self.notify("Run a search first (/)")
            return
        self._filter_mode = not self._filter_mode
        self._render_file_view()
        self._jump_to_current_match()

    def action_presets(self) -> None:
        """Show QE preset picker; on selection run the search."""
        if self.mode != "file_view":
            self.notify("Presets only work while viewing a file")
            return
        if not self.search_presets:
            self.notify("No presets available (check data/qe_patterns.json)")
            return

        def on_preset(preset: dict | None) -> None:
            if not preset:
                return
            pattern = preset.get("pattern", "").strip()
            if not pattern:
                return
            try:
                context = max(0, int(preset.get("context", 5)))
            except (TypeError, ValueError):
                context = 5
            self._run_file_search(pattern, context)

        self.push_screen(PresetScreen(self.search_presets), on_preset)

    def action_open_pager(self) -> None:
        """Suspend the app and open the current file in $PAGER (default 'less -R')."""
        if self.mode != "file_view" or not self.current_file:
            self.notify("Pager only works while viewing a file")
            return

        content = self._load_full_file_content()
        if not content:
            self.notify("No content to display", severity="warning")
            return

        pager = os.environ.get("PAGER") or ("less -R" if shutil.which("less") else None)
        if not pager:
            self.notify("No PAGER available (install 'less' or set $PAGER)", severity="error")
            return

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=f"_{self.current_file.replace('/', '_')}", delete=False
        )
        try:
            tmp.write(content)
            tmp.close()
            with self.suspend():
                subprocess.call(pager.split() + [tmp.name])
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter table rows as user types in search bar."""
        if event.input.id == "search_input" and self._search_active:
            if self._search_debounce_timer is not None:
                self._search_debounce_timer.stop()
            value = event.value
            self._search_debounce_timer = self.set_timer(
                0.15, lambda: self._apply_search_filter(value)
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Close search bar on Enter and return focus to table."""
        if event.input.id == "search_input":
            # Keep filter applied but close the input
            self._search_active = False
            event.input.visible = False
            self.table.focus()

    def on_key(self, event) -> None:
        """Handle Escape to close search bar or clear in-file search."""
        if event.key == "escape" and self._search_active:
            event.prevent_default()
            search_input = self.query_one("#search_input", Input)
            self._search_active = False
            search_input.visible = False
            search_input.value = ""
            self._apply_search_filter("")
            self.table.focus()
            return

        if (
            event.key == "escape"
            and self.mode == "file_view"
            and (self._search_pattern or self._filter_mode)
            and self.current_node
            and self.current_file
            and self.current_file_type
        ):
            event.prevent_default()
            self.show_file_content(
                self.current_node, self.current_file, self.current_file_type
            )

    def _row_matches_tag_filter(self, row: tuple) -> bool:
        """Return True if row passes the current tag filter (all/tagged/untagged)."""
        if self._tag_filter == "all":
            return True
        try:
            pk = int(row[0])
        except (ValueError, TypeError, IndexError):
            return True
        is_tagged = pk in self.tags
        return is_tagged if self._tag_filter == "tagged" else not is_tagged

    def _apply_search_filter(self, query: str) -> None:
        """Filter table rows by search query and active tag filter."""
        assert self.table is not None

        query_lower = query.lower().strip()

        if not query_lower:
            matching_rows = list(self._all_table_rows)
        else:
            matching_rows = [
                row
                for row, lower in zip(
                    self._all_table_rows, self._all_table_rows_lower
                )
                if query_lower in lower
            ]

        if self._tag_filter != "all" and self.mode in ("nodes", "descendants"):
            matching_rows = [
                row for row in matching_rows if self._row_matches_tag_filter(row)
            ]

        # First column is always PK in nodes/descendants modes
        if self.mode in ("nodes", "descendants"):
            new_nodes_list = []
            for row in matching_rows:
                try:
                    new_nodes_list.append(int(row[0]))
                except (ValueError, IndexError):
                    pass
            self.nodes_list = new_nodes_list
        else:
            self.nodes_list = []

        with self.batch_update():
            self.table.clear()
            if matching_rows:
                self.table.add_rows(matching_rows)

        # Reflect filter + search state in the title (without mutating the base title).
        if self.title_widget is not None:
            suffix_parts = []
            if self._tag_filter != "all" and self.mode in ("nodes", "descendants"):
                label = "Tagged only" if self._tag_filter == "tagged" else "Untagged only"
                suffix_parts.append(f"[b]{label}[/b]")
            if self._search_active:
                suffix_parts.append(
                    f"[b]Matches:[/b] {len(matching_rows)} / {len(self._all_table_rows)}"
                )
            if suffix_parts:
                self.title_widget.update(
                    f"{self._base_title} | " + " | ".join(suffix_parts)
                )
            else:
                self.title_widget.update(self._base_title)

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
