"""Main TUI application for browsing AiiDA groups and nodes."""

from __future__ import annotations

import datetime
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
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TextArea,
    Tree,
)
from textual.widgets.tree import TreeNode
from textual import work

from . import node_inspector, storage
from .classify import Classifier, ClassifierError, dump_classifiers, load_classifiers
from .scan import ScanBackend, ScanProgress, ScanRequest, ScanResult, run_scan
from .queries import (
    get_descendants,
    get_groups,
    get_nodes_in_group,
)

logger = logging.getLogger(__name__)

#: Rendering for process states, worst first — the node lists already sort this
#: way, but until now every row was the same colour.
STATE_STYLES = {
    "excepted": "bold red",
    "killed": "bold red",
    "finished": "green",
    "waiting": "yellow",
    "running": "yellow",
    "created": "dim",
}

#: Stable-ish palette for tag names, so clusters are visible in a long list.
TAG_PALETTE = (
    "bold yellow",
    "bold cyan",
    "bold magenta",
    "bold green",
    "bold blue",
    "bold bright_red",
    "bold bright_yellow",
    "bold bright_cyan",
)


def tag_style(tag: str) -> str:
    """Deterministic colour for a tag name."""
    return TAG_PALETTE[sum(map(ord, tag)) % len(TAG_PALETTE)]


def failure_priority(process_state: str | None, exit_status) -> int:
    """Lower sorts first. Excepted/killed are the most severe failures."""
    if process_state in ("excepted", "killed"):
        return 0
    if process_state == "finished" and exit_status:
        return 1
    if process_state == "finished":
        return 3
    if process_state:
        return 2
    return 4  # data nodes


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


class ClassifierScreen(ModalScreen[dict | None]):
    """Define the rule behind a tag: substring, regex, or exit code."""

    CSS = """
    ClassifierScreen {
        align: center middle;
    }

    #dialog {
        width: 84;
        height: 20;
        border: thick $background 80%;
        background: $surface;
    }

    #question {
        height: 2;
        content-align: center middle;
    }

    #kind_hint {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }

    Input {
        margin: 0 2;
    }
    """

    KINDS = ("substring", "regex", "exit_code", "empty_file")

    def __init__(
        self, tag_name: str, filename: str, *, kind: str = "substring", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.tag_name = tag_name
        self.filename = filename
        self._kind_index = self.KINDS.index(kind)
        self._case_sensitive = False

    @property
    def kind(self) -> str:
        return self.KINDS[self._kind_index]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(
                f"Rule for tag [b]{self.tag_name}[/b] — file [b]{self.filename}[/b]",
                id="question",
            ),
            Label("", id="kind_hint"),
            Input(placeholder="pattern, or exit code for the exit_code kind", id="pattern_input"),
            Label("", id="case_hint"),
            id="dialog",
        )

    def on_mount(self) -> None:
        self._refresh_hints()
        self.query_one("#pattern_input", Input).focus()

    def _refresh_hints(self) -> None:
        self.query_one("#kind_hint", Label).update(
            f"kind: [b]{self.kind}[/b]  ·  Tab cycles kind  ·  Ctrl+T toggles case"
        )
        case = "sensitive" if self._case_sensitive else "insensitive"
        detail = {
            "substring": f"plain text match, case {case}",
            "regex": f"regular expression, case {case}",
            "exit_code": "matches the failing CalcJob's exit status — no file is read",
            "empty_file": f"matches when {self.filename} is blank — just press Enter",
        }[self.kind]
        self.query_one("#case_hint", Label).update(f"  {detail}")

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)
        elif event.key == "tab":
            event.prevent_default()
            event.stop()
            self._kind_index = (self._kind_index + 1) % len(self.KINDS)
            self._refresh_hints()
        elif event.key == "ctrl+t":
            event.prevent_default()
            event.stop()
            self._case_sensitive = not self._case_sensitive
            self._refresh_hints()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if self.kind == "empty_file":
            self.dismiss({"kind": "empty_file"})
            return
        if not value:
            self.dismiss(None)
            return
        if self.kind == "exit_code":
            try:
                code = int(value)
            except ValueError:
                self.notify("Exit code must be an integer", severity="error")
                return
            self.dismiss({"kind": "exit_code", "exit_code": code})
            return
        self.dismiss(
            {
                "kind": self.kind,
                "pattern": value,
                "case_sensitive": self._case_sensitive,
            }
        )


class ExitCodeScreen(ModalScreen[dict | None]):
    """Tag by exit status — one code, or every distinct code at once."""

    CSS = """
    ExitCodeScreen {
        align: center middle;
    }

    #dialog {
        width: 76;
        height: 15;
        border: thick $background 80%;
        background: $surface;
    }

    #question {
        height: 3;
        content-align: center middle;
    }

    #hint {
        height: 2;
        content-align: center middle;
        color: $text-muted;
    }

    Input {
        margin: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Tag failures by exit code", id="question"),
            Input(placeholder="e.g. 305", id="code_input"),
            Input(placeholder="tag name (blank = 'exit <code>')", id="tag_input"),
            Label(
                "Enter to apply  ·  Ctrl+A to auto-tag every distinct exit code\n"
                "Exit codes need no file reads, so this classifies a whole group fast.",
                id="hint",
            ),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#code_input", Input).focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)
        elif event.key == "ctrl+a":
            event.prevent_default()
            event.stop()
            self.dismiss({"auto": True})

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = self.query_one("#code_input", Input).value.strip()
        if not raw:
            self.dismiss(None)
            return
        try:
            code = int(raw)
        except ValueError:
            self.notify("Exit code must be an integer", severity="error")
            return
        tag = self.query_one("#tag_input", Input).value.strip() or f"exit {code}"
        self.dismiss({"exit_code": code, "tag": tag})


class HelpScreen(ModalScreen):
    """Key bindings, grouped by where they work."""

    CSS = """
    HelpScreen {
        align: center middle;
    }

    #dialog {
        width: 84;
        height: 30;
        border: thick $background 80%;
        background: $surface;
    }

    #help_title {
        height: 1;
        content-align: center middle;
    }

    DataTable {
        height: 1fr;
    }
    """

    SECTIONS = (
        ("Everywhere", [
            ("a / Enter", "Select — drill down"),
            ("w", "Workflow tree panel on the right"),
            ("b / Backspace", "Back"),
            ("r", "Refresh the current view"),
            ("?", "This help"),
            ("q", "Quit"),
        ]),
        ("Node lists", [
            ("/", "Filter rows as you type"),
            ("T", "Cycle tag filter: all → tagged → untagged"),
            ("d", "Failure summary for the row under the cursor"),
            ("w", "Toggle the workflow tree (verdi process status)"),
            ("W", "Focus the tree — Enter opens a process or data node"),
            ("D", "Show/hide data nodes (inputs, outputs) in the tree"),
            ("v", "Inspect the data node under the cursor"),
            ("E", "Tag by exit code (Ctrl+A inside: auto-tag all)"),
            ("u", "Re-scan the group with every saved rule"),
            ("x", "Remove all tags from this row"),
            ("i", "Tag inspector — counts and rules"),
            ("S", "Statistics for this group"),
            ("e", "Export tagged PKs"),
            ("Escape", "Cancel a running scan"),
        ]),
        ("File view", [
            ("/", "Search within the file"),
            ("n / N", "Next / previous match"),
            ("L", "Last match"),
            ("F", "Toggle filtered view (matches + context)"),
            ("p", "Search presets"),
            ("f", "Open in $PAGER"),
            ("m / l", "More / fewer preview lines"),
            ("t", "Create a tag rule from this file"),
        ]),
    )

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("[b]Keys[/b] — Escape to close", id="help_title"),
            DataTable(zebra_stripes=True),
            id="dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Where", "Key", "Action")
        for section, entries in self.SECTIONS:
            first = True
            for key, description in entries:
                table.add_row(
                    Text(section, style="bold") if first else "",
                    Text(key, style="bold cyan"),
                    description,
                )
                first = False
        table.focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss(None)


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
        counts: dict[str, int],
        classifiers: list,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._counts = counts
        self._classifiers = classifiers

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
        table.add_columns("Tag", "Count", "Kind", "File", "Rule")

        by_tag = {c.tag: c for c in self._classifiers}
        rows = []
        for tag_name in sorted(set(self._counts) | set(by_tag)):
            classifier = by_tag.get(tag_name)
            rows.append(
                (
                    Text(tag_name, style=tag_style(tag_name)),
                    str(self._counts.get(tag_name, 0)),
                    classifier.kind if classifier else "—",
                    (classifier.filename or "—") if classifier else "—",
                    classifier.describe() if classifier else "(no rule saved)",
                )
            )
        if rows:
            table.add_rows(rows)
        table.focus()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
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
            event.stop()
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
            event.stop()
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

        #body {
            height: 1fr;
        }

        #main_pane {
            width: 1fr;
        }

        #workflow_pane {
            width: 0;
            display: none;
            border-left: solid $primary;
        }

        #workflow_pane.visible {
            width: 40%;
            min-width: 44;
            display: block;
        }

        #workflow_header {
            height: 2;
            padding: 0 1;
            color: $text-muted;
        }

        #workflow_tree {
            height: 1fr;
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
        Binding("question_mark", "help", "Help", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("a", "select", "Select", show=True),
        Binding("enter", "select", "Select", show=False),
        Binding("b", "go_back", "Back", show=True),
        Binding("backspace", "go_back", "Back", show=False),
        Binding("m", "increase_preview", "More lines", show=True),
        Binding("l", "decrease_preview", "Fewer lines", show=True),
        Binding("t", "tag_error", "Tag Error", show=True),
        Binding("T", "filter_by_tag", "Filter tagged", show=True),
        Binding("E", "tag_by_exit_code", "Tag by exit code", show=True),
        Binding("d", "failure_summary", "Why did it fail?", show=True),
        Binding("S", "statistics", "Stats", show=True),
        Binding("w", "toggle_workflow", "Workflow tree", show=True),
        Binding("W", "focus_workflow", "Focus tree", show=False),
        Binding("D", "toggle_data_nodes", "Data nodes", show=False),
        Binding("v", "inspect_data", "Inspect node", show=True),
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
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
    ]

    #: Which actions make sense in which modes. Drives both the footer and the
    #: guard in check_action, so keys stop advertising themselves where they do
    #: nothing.
    MODE_ACTIONS = {
        "tag_error": {"file_view"},
        "increase_preview": {"file_view"},
        "decrease_preview": {"file_view"},
        "presets": {"file_view"},
        "open_pager": {"file_view"},
        "next_match": {"file_view"},
        "prev_match": {"file_view"},
        "last_match": {"file_view"},
        "toggle_filter": {"file_view"},
        "filter_by_tag": {"nodes"},
        "tag_by_exit_code": {"nodes", "descendants"},
        "failure_summary": {"nodes", "descendants"},
        "untag": {"nodes", "descendants"},
        "export_tagged": {"nodes", "descendants"},
        "statistics": {"nodes", "descendants"},
        "update_tags": {"nodes", "descendants", "file_list", "file_view"},
        "cursor_top": {"groups", "nodes", "descendants", "file_list"},
        "cursor_bottom": {"groups", "nodes", "descendants", "file_list"},
        "toggle_workflow": {"nodes", "descendants", "file_list", "file_view"},
        "focus_workflow": {"nodes", "descendants", "file_list", "file_view"},
        "toggle_data_nodes": {"nodes", "descendants", "file_list", "file_view"},
        "inspect_data": {"nodes", "descendants"},
    }

    def check_action(self, action: str, parameters) -> bool | None:
        """Hide bindings that do nothing in the current mode."""
        allowed = self.MODE_ACTIONS.get(action)
        if allowed is None:
            return True
        return self.mode in allowed

    def __init__(
        self,
        group_identifier: str | None = None,
        data_dir: str | Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.group_identifier = group_identifier
        self.group = None
        self.table: Optional[DataTable] = None
        self.title_widget: Optional[Static] = None

        # "groups", "nodes", "descendants", "file_list", "file_view", "panel"
        self.mode = "groups"
        self._panel_return_mode = "nodes"
        self.groups = []
        self.current_node: Optional[Node] = None
        self.root_node: Optional[Node] = None  # The workchain selected from nodes list
        self.navigation_stack: list[tuple[str, Optional[Node]]] = (
            []
        )  # Stack of (mode, node) pairs
        self.selected_group_label: str | None = None  # Restore cursor by label, not index
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
        self.current_file_blank = False  # open file is empty / whitespace only

        # Settings - show last 500 lines by default for files
        self.preview_lines = 500
        #: How many failing CalcJobs per workchain a scan inspects. The old code
        #: looked at only the most recent one, which is wrong for restart-driven
        #: workchains where the last attempt is not the characteristic failure.
        self.max_calcjobs = 5
        #: How deep the call-graph walk goes, for both scanning and the panel.
        self.max_depth = 8
        #: The `verdi process status` panel beside the table.
        self.show_workflow = False
        #: Include data nodes (structures, parameters, ...) in the tree.
        self.show_data_nodes = True
        self.workflow_tree: Optional[Tree] = None
        self._workflow_pk: int | None = None
        self._workflow_timer = None

        # Search/filter state
        self._all_table_rows: list[tuple] = []  # Unfiltered rows for current table view
        self._all_table_rows_lower: list[str] = []  # Pre-computed lowercase joins for fast filtering
        self._search_active = False
        self._search_debounce_timer = None
        self._base_title = ""  # Title without search match-count suffix
        self._tag_filter = "all"  # "all", "tagged", "untagged" — cycled via T

        # Scanning state
        self._scanning = False

        data_dir = storage.resolve_data_dir(data_dir)
        self.data_dir = data_dir

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
        #: Scroll mode maps display=header+source, so the dict above is unused.
        self._affine_line_map: bool = True
        self._file_header_line_count: int = 0  # how many header lines prepend the content

        self.presets_file = data_dir / "qe_patterns.json"
        self.search_presets: list[dict] = []
        self._load_or_seed_presets()

        self.tags_file = data_dir / "tags.json"
        self.tags: dict[int, set[str]] = {}  # pk -> {tag, ...}; a node may carry several
        self.scan_cache_file = data_dir / "scanned.json"
        self.legacy_categorized_file = data_dir / "categorized.json"
        #: pk -> classifier fingerprints already applied. Unlike the old
        #: ``categorized.json`` (matches only), this also remembers *misses*, so
        #: unmatched nodes are not re-read on every re-scan.
        self.scan_cache: dict[int, set[str]] = {}
        self.patterns_file = data_dir / "patterns.json"
        self.classifiers: list[Classifier] = []
        self.settings_file = data_dir / "settings.json"
        self._pending_notices: list[tuple[str, str]] = []

        self.load_tags()
        self.load_scan_cache()
        self.load_patterns()
        self.load_settings()

    def compose(self) -> ComposeResult:
        yield Header()
        self.title_widget = Static(id="title")
        yield self.title_widget
        with Horizontal(id="body"):
            with Vertical(id="main_pane"):
                self.table = DataTable(zebra_stripes=True)
                yield self.table
                self.detail_view = TextArea(read_only=True)
                self.detail_view.display = False
                yield self.detail_view
            with Vertical(id="workflow_pane"):
                yield Static("", id="workflow_header")
                self.workflow_tree = Tree("workflow", id="workflow_tree")
                self.workflow_tree.show_root = True
                self.workflow_tree.guide_depth = 3
                yield self.workflow_tree
        yield Input(placeholder="Search... (Escape to close)", id="search_input")
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app is ready."""
        self._flush_notices()
        if self.show_workflow:
            self.query_one("#workflow_pane").add_class("visible")
        self.show_group_list()

    def on_data_table_row_highlighted(self, event) -> None:
        """Keep the workflow panel pointed at whatever the cursor is on."""
        self.refresh_workflow_panel()

    # ------------------------------------------------------------------ #
    # Persistence. All writes go through storage.atomic_write_json, so an
    # interrupt mid-save can no longer truncate months of tagging to nothing.
    # ------------------------------------------------------------------ #

    def _save(self, path: Path, payload) -> bool:
        try:
            storage.atomic_write_json(path, payload)
            return True
        except OSError as exc:
            logger.error("Could not write %s: %s", path, exc)
            self._notify_later(f"Could not write {path.name}: {exc}", "error")
            return False

    def _notify_later(self, message: str, severity: str = "warning") -> None:
        """Queue a notification raised before the app is mounted."""
        if self.is_running:
            self.notify(message, severity=severity)
        else:
            self._pending_notices.append((message, severity))

    def _flush_notices(self) -> None:
        for message, severity in self._pending_notices:
            self.notify(message, severity=severity)
        self._pending_notices.clear()

    def load_tags(self) -> None:
        data, quarantined = storage.load_json(self.tags_file, {})
        if quarantined is not None:
            self._notify_later(
                f"tags.json was unreadable; kept as {quarantined.name}", "error"
            )
        self.tags = storage.parse_tags(data)

    def save_tags(self) -> None:
        self._save(self.tags_file, storage.serialise_tags(self.tags))

    def load_scan_cache(self) -> None:
        """Load the negative cache, migrating from categorized.json once."""
        if self.scan_cache_file.exists():
            data, quarantined = storage.load_json(self.scan_cache_file, {})
            if quarantined is not None:
                self._notify_later(
                    f"scanned.json was unreadable; kept as {quarantined.name}", "error"
                )
            self.scan_cache = storage.parse_scan_cache(data)
            return

        if self.legacy_categorized_file.exists():
            data, _ = storage.load_json(self.legacy_categorized_file, [])
            self.scan_cache = storage.parse_scan_cache(data)
            logger.info(
                "Migrated %s entries from categorized.json", len(self.scan_cache)
            )
        else:
            self.scan_cache = {}

    def save_scan_cache(self) -> None:
        self._save(self.scan_cache_file, storage.serialise_scan_cache(self.scan_cache))

    def load_patterns(self) -> None:
        data, quarantined = storage.load_json(self.patterns_file, {})
        if quarantined is not None:
            self._notify_later(
                f"patterns.json was unreadable; kept as {quarantined.name}", "error"
            )
        self.classifiers, errors = load_classifiers(data)
        for message in errors:
            logger.warning("Ignoring classifier: %s", message)
            self._notify_later(f"Ignoring classifier: {message}", "warning")

    def save_patterns(self) -> None:
        self._save(self.patterns_file, dump_classifiers(self.classifiers))

    def classifier_for(self, tag: str) -> Classifier | None:
        for classifier in self.classifiers:
            if classifier.tag == tag:
                return classifier
        return None

    def upsert_classifier(self, classifier: Classifier) -> None:
        """Replace any rule with the same tag, then persist."""
        self.classifiers = [c for c in self.classifiers if c.tag != classifier.tag]
        self.classifiers.append(classifier)
        self.save_patterns()

    def load_settings(self) -> None:
        data, _ = storage.load_json(self.settings_file, {})
        if not isinstance(data, dict):
            return
        value = data.get("preview_lines")
        if isinstance(value, int) and value > 0:
            self.preview_lines = value
        value = data.get("max_calcjobs")
        if isinstance(value, int) and value > 0:
            self.max_calcjobs = value
        value = data.get("max_depth")
        if isinstance(value, int) and value > 0:
            self.max_depth = value
        self.show_workflow = bool(data.get("show_workflow", False))
        self.show_data_nodes = bool(data.get("show_data_nodes", True))

    def save_settings(self) -> None:
        self._save(
            self.settings_file,
            {
                "preview_lines": self.preview_lines,
                "max_calcjobs": self.max_calcjobs,
                "max_depth": self.max_depth,
                "show_workflow": self.show_workflow,
                "show_data_nodes": self.show_data_nodes,
            },
        )

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

    def _tag_cell(self, pk: int):
        """Render every tag on a node. A node can now carry more than one."""
        tags = sorted(self.tags.get(pk, ()))
        if not tags:
            return "-"
        cell = Text()
        for index, tag in enumerate(tags):
            if index:
                cell.append(", ", style="dim")
            cell.append(tag, style=tag_style(tag))
        return cell

    @staticmethod
    def _state_cell(state):
        """Colour the process state: red for excepted/killed, green for finished."""
        text = str(state) if state else "-"
        style = STATE_STYLES.get(text)
        return Text(text, style=style) if style else text

    @staticmethod
    def _exit_cell(exit_status):
        """Non-zero exit codes are the interesting ones; make them visible."""
        text = str(exit_status) if exit_status not in (None, "") else "-"
        if text in ("-", "0"):
            return Text(text, style="dim")
        return Text(text, style="bold yellow")

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
        """Populate the table with available groups.

        The query runs on a worker thread: counting group membership touches
        every (group, node) pair, which on a large profile made the app look
        frozen at startup with no feedback at all.
        """
        assert self.table is not None
        self._dismiss_search()

        self.mode = "groups"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Label", "Type", "#Nodes")
        self._set_table_rows([])
        self._set_breadcrumb_title("[b yellow]Loading groups…[/b yellow]")
        self.table.loading = True
        self._load_groups_worker()

    @work(thread=True, exclusive=True, group="load")
    def _load_groups_worker(self) -> None:
        try:
            groups = get_groups()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Loading groups failed")
            self.call_from_thread(self._groups_failed, str(exc))
            return
        self.call_from_thread(self._groups_loaded, groups)

    def _groups_failed(self, message: str) -> None:
        assert self.table is not None
        self.table.loading = False
        self._set_title(f"[b red]Could not list groups: {message}[/b red]")

    def _groups_loaded(self, groups: list[dict]) -> None:
        assert self.table is not None
        self.table.loading = False
        if self.mode != "groups":
            return  # the user navigated away while we were loading
        self.groups = groups
        rows = [
            (g["label"], g["type_string"], str(g["n_nodes"])) for g in self.groups
        ]
        self._set_table_rows(rows)

        if self.groups:
            self._set_breadcrumb_title(f"{len(self.groups)} group(s) · 'a' to open")
        else:
            self._set_breadcrumb_title("[b yellow]No core groups found[/b yellow]")

        if self.groups:
            self.table.focus()
            if self.selected_group_label:
                for index, group in enumerate(self.groups):
                    if group["label"] == self.selected_group_label:
                        self.table.move_cursor(row=index)
                        break

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
        """Load the group's nodes on a worker thread."""
        assert self.table is not None
        assert self.group is not None
        self._dismiss_search()
        self.table.loading = True
        self._set_breadcrumb_title("[b yellow]Loading nodes…[/b yellow]")
        self._load_nodes_worker(self.group.label)

    @work(thread=True, exclusive=True, group="load")
    def _load_nodes_worker(self, group_label: str) -> None:
        try:
            results = get_nodes_in_group(group_label)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Loading nodes failed")
            self.call_from_thread(self._nodes_failed, str(exc))
            return
        self.call_from_thread(self._nodes_loaded, results)

    def _nodes_failed(self, message: str) -> None:
        assert self.table is not None
        self.table.loading = False
        self._set_title(f"[b red]Could not load nodes: {message}[/b red]")

    def _nodes_loaded(self, results: list) -> None:
        assert self.table is not None
        self.table.loading = False
        if self.mode != "nodes":
            return

        # Failures first, then by PK — see failure_priority().
        results = sorted(
            results,
            key=lambda row: (failure_priority(row[5], row[6]), row[0]),
        )
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
            if "StructureData" in node_type:
                row_type = formula or "Structure"
                row_state = None
                row_exit = None
            else:
                row_type = process_label
                row_state = process_state
                row_exit = exit_status

            rows.append(
                (
                    str(pk),
                    uuid[:8],
                    row_type,
                    self._state_cell(row_state),
                    self._exit_cell(row_exit),
                    self._tag_cell(pk),
                )
            )

        self._set_table_rows(rows)

        tagged = sum(1 for pk in self.nodes_list if pk in self.tags)
        self._set_breadcrumb_title(
            f"[b]Nodes:[/b] {len(results)}  ·  [b]tagged:[/b] {tagged}"
        )
        if self._tag_filter != "all":
            self._apply_search_filter("")

        if results:
            self.table.focus()
            if self.selected_node_pk and self.selected_node_pk in self.nodes_list:
                self.table.move_cursor(row=self.nodes_list.index(self.selected_node_pk))

    def show_descendants(self, node: Node) -> None:
        """Display the WorkChains and CalcJobs this node called."""
        assert self.table is not None
        self._dismiss_search()

        self.mode = "descendants"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("PK", "Process", "State", "Exit code", "Tag")

        # Execution order (PK = creation order), not failures-first: these rows
        # are the steps of one workchain, e.g. a PwBase and its restarts.
        descendants = sorted(get_descendants(node), key=lambda row: row["pk"])
        self.nodes_list = [row["pk"] for row in descendants]

        rows = [
            (
                str(row["pk"]),
                row["process_label"] or row["node_type"].rstrip(".").rsplit(".", 1)[-1],
                self._state_cell(row["process_state"]),
                self._exit_cell(row["exit_status"]),
                self._tag_cell(row["pk"]),
            )
            for row in descendants
        ]
        self._set_table_rows(rows)

        if descendants:
            self._set_breadcrumb_title(f"[b]Processes:[/b] {len(descendants)}")
        else:
            self._set_breadcrumb_title(
                "[b yellow]No called processes — this node ran nothing[/b yellow]"
            )
        # The tag filter deliberately does NOT apply here. Only father
        # workchains that are group members ever get tagged, so filtering
        # descendants by 'tagged' hid every row and left an empty table —
        # the reported shift+T bug.

        if self.nodes_list:
            self.table.focus()
            selected_child_pk = self.selected_descendants.get(node.pk)
            if selected_child_pk in self.nodes_list:
                self.table.move_cursor(row=self.nodes_list.index(selected_child_pk))

    def show_file_list(self, node: Node) -> bool:
        """List every file the calculation retrieved, plus its inputs.

        Returns False when there is nothing to show, so the caller can avoid
        pushing a navigation-stack entry for a view that never appeared.

        Previously only three hardcoded output filenames were listed, which is
        why Quantum ESPRESSO's ``CRASH`` file — the canonical error file — could
        not be opened at all.
        """
        assert self.table is not None

        if not isinstance(node, orm.CalcJobNode):
            self.notify("Not a CalcJob — no files available")
            return False

        files = node_inspector.list_all_files(node)
        if not files:
            # Common for calculations that never ran; say so rather than
            # leaving an unexplained empty table.
            self._dismiss_search()
            self.mode = "file_list"
            self.available_files = []
            self.table.clear(columns=True)
            self.table.cursor_type = "row"
            self.table.add_columns("Filename", "Type", "Size")
            self._set_table_rows([])
            self._set_breadcrumb_title(
                "[b yellow]No retrieved or input files — the calculation may never have run[/b yellow]"
            )
            return True

        self._dismiss_search()
        self.mode = "file_list"
        self.table.clear(columns=True)
        self.table.cursor_type = "row"
        self.table.add_columns("Filename", "Type", "Size")

        self.available_files = files
        rows: list[tuple] = []
        for filename, file_type in files:
            size, capped = node_inspector.file_size(node, filename, file_type)
            rows.append((filename, file_type, node_inspector.format_size(size, capped)))

        self._set_table_rows(rows)
        self._set_breadcrumb_title(f"{len(files)} file(s) · 'a' to view")

        self.table.focus()
        if node.pk in self.selected_files:
            wanted = self.selected_files[node.pk]
            for idx, (filename, _) in enumerate(self.available_files):
                if filename == wanted:
                    self.table.move_cursor(row=idx)
                    break
        return True

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

        # One streaming pass produces the tail (outputs) or whole file (inputs).
        if file_type == "output":
            preview = node_inspector.read_preview(
                node, filename, "output", head=0, tail=self.preview_lines
            )
            shown = min(preview.total_lines, self.preview_lines)
            header = "=" * 80 + "\n"
            header += (
                f"FILE: {filename} — last {shown} of {preview.total_lines} lines\n"
            )
        else:
            # Inputs are small; show them whole.
            text, truncated = node_inspector.read_text(node, filename, "input")
            lines = text.splitlines()
            preview = node_inspector.Preview([], lines, len(lines), 0, truncated)
            header = "=" * 80 + "\n"
            header += f"FILE: {filename} (input file)\n"
        header += "=" * 80 + "\n"

        self.current_file_blank = (
            not preview.omitted
            and not preview.truncated
            and not any(line.strip() for line in preview.head + preview.tail)
        )
        if self.current_file_blank:
            header += "[empty file — press 't' to tag every workchain where it is empty]\n"
        self._file_header_line_count = header.count("\n")

        self.detail_view.text = header + preview.render()

        if file_type == "output":
            self._set_breadcrumb_title(
                f"Last {shown} lines | m/l adjust | '/' search | 'f' pager | 'b' back"
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
        self._affine_line_map = True
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
            {"name": "Error in routine", "pattern": "Error in routine", "context": 10},
            {"name": "SCF iteration", "pattern": "iteration #", "context": 1},
        ]
        if not self.presets_file.exists():
            self.search_presets = default_presets
            self._save(self.presets_file, {"presets": default_presets})
            return

        data, quarantined = storage.load_json(self.presets_file, {})
        if quarantined is not None:
            self._notify_later(
                f"qe_patterns.json was unreadable; kept as {quarantined.name}", "warning"
            )
            self.search_presets = default_presets
            return
        presets = data.get("presets", []) if isinstance(data, dict) else []
        self.search_presets = [
            p for p in presets if isinstance(p, dict) and p.get("pattern")
        ] or default_presets

    def _load_full_file_content(self) -> str:
        """Return the full text of the currently viewed file (cached)."""
        if not self.current_node or not self.current_file or not self.current_file_type:
            return ""
        key = (self.current_node.pk, self.current_file, self.current_file_type)
        if key in self._file_content_cache:
            return self._file_content_cache[key]
        content, truncated = node_inspector.read_text(
            self.current_node, self.current_file, self.current_file_type
        )
        if truncated:
            self.notify(
                "File is large; search covers the first 16 MB. Press 'f' for the pager.",
                severity="warning",
            )
        # Bound the cache: whole files were previously kept for the whole
        # session, so browsing many large outputs pinned them all in memory.
        if len(self._file_content_cache) >= 8:
            self._file_content_cache.pop(next(iter(self._file_content_cache)))
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
        # In scroll mode the mapping is affine (display = header + source), so
        # there is no need to materialise a list and a dict entry per line —
        # that was O(lines) work and memory on every render, for files that can
        # run to hundreds of thousands of lines.
        self._display_to_source = []
        self._source_to_display = {}
        self._affine_line_map = True
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
        self._affine_line_map = False
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

    def _display_line_for(self, source_line: int) -> int | None:
        """Map a source line to its rendered line."""
        if self._affine_line_map:
            if 0 <= source_line < len(self._file_full_lines):
                return self._file_header_line_count + source_line
            return None
        return self._source_to_display.get(source_line)

    def _jump_to_current_match(self) -> None:
        """Scroll to and highlight the current match."""
        assert self.detail_view is not None
        if not self._search_matches or self._search_current < 0:
            return
        src_line = self._search_matches[self._search_current]
        disp_line = self._display_line_for(src_line)
        if disp_line is None:
            # Re-render if match isn't in current display (shouldn't happen, guard anyway)
            self._render_file_view()
            disp_line = self._display_line_for(src_line)
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
        if row_index is None or not self.table.row_count:
            # cursor_row is 0 rather than None on an empty table, so guarding
            # on None alone let get_row_at(0) raise RowDoesNotExist.
            return
        if row_index >= self.table.row_count:
            return

        if self.mode == "groups":
            # Select group -> show nodes
            row = self.table.get_row_at(row_index)
            group_label = str(row[0])
            # Store the label, not the row index: the index refers to the
            # possibly search-filtered table but was used to index the
            # unfiltered self.groups, landing on an unrelated group.
            self.selected_group_label = group_label
            self.group_identifier = group_label
            self.load_group()
            # Mode must be set BEFORE loading: load_nodes renders the
            # breadcrumb and re-applies filters, both of which branch on it.
            self.mode = "nodes"
            self.setup_table()
            self.load_nodes()

        elif self.mode in ["nodes", "descendants"]:
            # Select node -> show descendants or files (if CalcJob)
            row = self.table.get_row_at(row_index)
            try:
                node_pk = int(row[0])
            except (TypeError, ValueError):
                return
            try:
                selected_node = orm.load_node(node_pk)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Could not load node {node_pk}: {exc}", severity="error")
                return

            is_calcjob = isinstance(selected_node, orm.CalcJobNode)
            if not is_calcjob and not isinstance(selected_node, orm.ProcessNode):
                # Data nodes — structures above all — are worth inspecting.
                # Groups in a high-throughput campaign often hold the
                # StructureData directly rather than the workchains.
                self.inspect_data_node(node_pk)
                return

            # Work out what the new view would be BEFORE mutating navigation
            # state, so a view that never appears cannot leave a stale entry on
            # the stack (which then made 'b' behave oddly).
            previous_mode, previous_node = self.mode, self.current_node

            if is_calcjob:
                self.current_node = selected_node
                if not self.show_file_list(selected_node):
                    self.current_node = previous_node
                    return
            else:
                self.current_node = selected_node
                self.show_descendants(selected_node)

            if previous_mode == "nodes":
                self.root_node = selected_node
                self.selected_node_pk = node_pk
                self.navigation_stack.append(("nodes", None))
            else:
                self.navigation_stack.append((previous_mode, previous_node))
                if previous_node is not None:
                    self.selected_descendants[previous_node.pk] = node_pk

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
        if self.mode != "file_view" or self.current_file_type != "output":
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
        if self.mode != "file_view" or self.current_file_type != "output":
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
        if self.mode == "panel":
            # Read-only overlay (failure summary / statistics): just dismiss it.
            assert self.detail_view is not None and self.table is not None
            self.detail_view.display = False
            self.table.display = True
            self.mode = self._panel_return_mode
            self.table.focus()
            self._set_breadcrumb_title()
            return

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
        """Create a classifier from the file on screen and scan the group with it."""
        if self.mode != "file_view" or not self.current_file:
            self.notify("Open a file first, then press 't'")
            return
        if not self.group:
            return

        filename = self.current_file

        def on_tag_name(tag_name: str | None) -> None:
            if not tag_name or not tag_name.strip():
                return
            tag_name = tag_name.strip()

            def on_rule(result: dict | None) -> None:
                if not result:
                    return
                try:
                    classifier = Classifier(
                        tag=tag_name,
                        kind=result["kind"],
                        filename=filename if result["kind"] != "exit_code" else None,
                        pattern=result.get("pattern"),
                        case_sensitive=result.get("case_sensitive", False),
                        exit_code=result.get("exit_code"),
                    )
                except ClassifierError as exc:
                    self.notify(str(exc), severity="error")
                    return
                self.upsert_classifier(classifier)
                self.start_scan([classifier], navigate_home=True)

            kind = "empty_file" if self.current_file_blank else "substring"
            self.push_screen(ClassifierScreen(tag_name, filename, kind=kind), on_rule)

        self.push_screen(TagNameScreen(), on_tag_name)

    def action_tag_by_exit_code(self) -> None:
        """Classify every failure in the group by its exit status — no file reads."""
        if not self.group:
            self.notify("Open a group first", severity="warning")
            return

        def on_result(result: dict | None) -> None:
            if not result:
                return
            if result.get("auto"):
                self.start_auto_exit_code_scan(self.group.label)
                return
            try:
                classifier = Classifier(
                    tag=result["tag"], kind="exit_code", exit_code=result["exit_code"]
                )
            except ClassifierError as exc:
                self.notify(str(exc), severity="error")
                return
            self.upsert_classifier(classifier)
            self.start_scan([classifier], navigate_home=True)

        self.push_screen(ExitCodeScreen(), on_result)

    @work(thread=True, exclusive=True, group="scan")
    def start_auto_exit_code_scan(self, group_label: str) -> None:
        """Build one exit-code classifier per distinct failing exit status, then scan.

        This costs no file reads at all, so it can classify a whole group at
        query speed — including the workchains that have no retrievable output.
        """
        from . import traversal

        try:
            fathers = traversal.failed_workchains_in_group(group_label)
            forest = traversal.call_forest([f.pk for f in fathers])
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Could not read the group: {exc}", severity="error"
            )
            return

        codes: set[int] = set()
        for father in fathers:
            if father.exit_status:
                codes.add(father.exit_status)
            for ref in forest.get(father.pk, []):
                if ref.is_calcjob and ref.exit_status:
                    codes.add(ref.exit_status)

        if not codes:
            self.call_from_thread(self.notify, "No exit codes found to classify")
            return

        classifiers = [
            Classifier(tag=f"exit {code}", kind="exit_code", exit_code=code)
            for code in sorted(codes)
        ]
        self.call_from_thread(self._register_classifiers, classifiers)
        self._scan_in_thread(group_label, classifiers, navigate_home=True)

    def _register_classifiers(self, classifiers: list[Classifier]) -> None:
        for classifier in classifiers:
            self.classifiers = [c for c in self.classifiers if c.tag != classifier.tag]
            self.classifiers.append(classifier)
        self.save_patterns()

    def action_update_tags(self) -> None:
        """Re-scan the group with every saved classifier."""
        if not self.group:
            self.notify("No group selected. Navigate to a group first.", severity="error")
            return
        if not self.classifiers:
            self.notify("No classifiers saved yet. Use 't' to create one.", severity="warning")
            return
        self.start_scan(self.classifiers, navigate_home=False)

    # ------------------------------------------------------------------ #
    # Scanning
    #
    # The worker owns only locals and hands back a single ScanResult, which
    # the UI thread merges. Previously the worker mutated self.tags and
    # self.categorized_workchains directly while the main thread iterated
    # them, and the re-scan path never saved at all.
    # ------------------------------------------------------------------ #

    def start_scan(self, classifiers: list[Classifier], *, navigate_home: bool) -> None:
        if not self.group:
            return
        if not classifiers:
            self.notify("Nothing to scan with", severity="warning")
            return
        # Read the label here: ORM objects are bound to the thread's DB session,
        # so self.group must never be touched from the worker.
        self._scan_worker(self.group.label, list(classifiers), navigate_home)

    @work(thread=True, exclusive=True, group="scan")
    def _scan_worker(
        self, group_label: str, classifiers: list[Classifier], navigate_home: bool
    ) -> None:
        self._scan_in_thread(group_label, classifiers, navigate_home=navigate_home)

    def _scan_in_thread(
        self, group_label: str, classifiers: list[Classifier], *, navigate_home: bool
    ) -> None:
        """Body of the scan worker. Never touches self.tags, self.scan_cache or
        any ORM object loaded on the UI thread."""
        from textual.worker import get_current_worker

        worker = get_current_worker()
        # Snapshot: the worker must not read a dict the UI thread can mutate.
        cache_snapshot = {pk: set(fps) for pk, fps in self.scan_cache.items()}

        request = ScanRequest(
            group_label=group_label,
            classifiers=tuple(classifiers),
            max_calcjobs=self.max_calcjobs,
            max_depth=self.max_depth,
        )

        def report(progress: ScanProgress) -> None:
            self.call_from_thread(self._on_scan_progress, progress)

        try:
            result = run_scan(
                request,
                backend=ScanBackend.default(),
                scan_cache=cache_snapshot,
                progress=report,
                should_cancel=lambda: worker.is_cancelled,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scan failed")
            self.call_from_thread(self.notify, f"Scan failed: {exc}", severity="error")
            return

        self.call_from_thread(self._apply_scan_result, result, navigate_home)

    def _on_scan_progress(self, progress: ScanProgress) -> None:
        if progress.total:
            pct = 100 * progress.done // max(1, progress.total)
            self._set_title(
                f"[b yellow]Scanning… {progress.done}/{progress.total} ({pct}%) "
                f"{progress.current}  ·  Escape to cancel[/b yellow]"
            )

    def _apply_scan_result(self, result: ScanResult, navigate_home: bool) -> None:
        """Merge a finished scan on the UI thread and persist it."""
        for pk, tags in result.new_tags.items():
            self.tags.setdefault(pk, set()).update(tags)
        for pk, fingerprints in result.scanned.items():
            self.scan_cache.setdefault(pk, set()).update(fingerprints)

        self.save_tags()
        self.save_scan_cache()

        if navigate_home:
            self._goto_nodes_view()
        elif self.mode == "nodes":
            self.load_nodes()

        severity = "warning" if result.cancelled else "information"
        self.notify(result.summary(), severity=severity, timeout=10)
        self._set_title(f"[b green]{result.summary()}[/b green]")

    def action_cancel_scan(self) -> None:
        """Stop a running scan; partial results are still applied."""
        try:
            self.workers.cancel_group(self, "scan")
        except Exception:  # noqa: BLE001
            return
        self.notify("Cancelling scan…")

    def _goto_nodes_view(self) -> None:
        """Return to the group's node list from anywhere.

        Replaces ``while self.mode != "nodes": self.action_go_back()``, which
        spun forever when the mode was "groups" — ``action_go_back`` there calls
        exit() and leaves the mode unchanged.
        """
        if self.detail_view is not None:
            self.detail_view.display = False
        if self.table is not None:
            self.table.display = True
        self.navigation_stack.clear()
        self.root_node = None
        self.current_node = None
        self.current_file = None
        self.current_file_type = None
        if self.group is None:
            self.show_group_list()
            return
        self.mode = "nodes"
        self.setup_table()
        self.load_nodes()

    def action_filter_by_tag(self) -> None:
        """Cycle the tag-only filter: all → tagged → untagged → all."""
        if self.mode != "nodes":
            self.notify("Tag filter only applies to the group's node list")
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
        """Remove every tag from the row under the cursor."""
        if self.mode not in ("nodes", "descendants"):
            return
        if self.table is None or not self.table.row_count:
            return
        cursor = self.table.cursor_row
        if cursor is None or cursor >= self.table.row_count:
            return
        try:
            pk = int(self.table.get_row_at(cursor)[0])
        except (ValueError, IndexError, TypeError):
            return
        if pk not in self.tags:
            self.notify("Row is not tagged", severity="warning")
            return

        removed = sorted(self.tags.pop(pk))
        # Forget that this node was tested against the classifiers that produced
        # those tags, otherwise the negative cache would prevent it ever being
        # re-tagged.
        fingerprints = {
            c.fingerprint() for c in self.classifiers if c.tag in removed
        }
        if pk in self.scan_cache:
            self.scan_cache[pk] -= fingerprints
            if not self.scan_cache[pk]:
                del self.scan_cache[pk]

        self.save_tags()
        self.save_scan_cache()
        self.notify(f"Removed {', '.join(removed)} from PK {pk}")

        if self.mode == "nodes":
            self.load_nodes()
        elif self.mode == "descendants" and self.current_node:
            self.show_descendants(self.current_node)

    # ------------------------------------------------------------------ #
    # Workflow panel — a `verdi process status` tree beside the table.
    # ------------------------------------------------------------------ #

    def action_toggle_workflow(self) -> None:
        """Show or hide the call-graph panel."""
        self.show_workflow = not self.show_workflow
        self.save_settings()
        pane = self.query_one("#workflow_pane")
        pane.set_class(self.show_workflow, "visible")
        if self.show_workflow:
            self.refresh_workflow_panel(force=True)
        else:
            if self.table is not None:
                self.table.focus()

    def action_focus_workflow(self) -> None:
        """Jump focus into the tree, opening the panel if needed."""
        if not self.show_workflow:
            self.action_toggle_workflow()
        if self.workflow_tree is not None:
            self.workflow_tree.focus()

    def _workflow_subject_pk(self) -> int | None:
        """Which process the panel should describe, given where we are.

        In the node lists that is the row under the cursor; deeper in, it is
        the workchain we drilled in from, so the tree stays anchored to the
        whole workflow rather than collapsing as you descend.
        """
        if self.mode in ("nodes", "descendants"):
            pk = self._cursor_pk()
            if pk is not None:
                return pk
        if self.root_node is not None:
            return self.root_node.pk
        if self.current_node is not None:
            return self.current_node.pk
        return None

    def refresh_workflow_panel(self, *, force: bool = False) -> None:
        """Rebuild the tree for the current subject, debounced."""
        if not self.show_workflow or self.workflow_tree is None:
            return
        pk = self._workflow_subject_pk()
        if pk is None:
            self._render_workflow_placeholder("Select a process to see its call graph")
            return
        if pk == self._workflow_pk and not force:
            return
        self._workflow_pk = pk

        if self._workflow_timer is not None:
            self._workflow_timer.stop()
        # Moving the cursor with the arrow keys should not fire a query per row.
        self._workflow_timer = self.set_timer(
            0.2, lambda: self._load_workflow_tree(pk)
        )

    def _render_workflow_placeholder(self, message: str) -> None:
        if self.workflow_tree is None:
            return
        self.query_one("#workflow_header", Static).update(message)
        self.workflow_tree.reset("workflow")

    @work(thread=True, exclusive=True, group="workflow")
    def _load_workflow_tree(self, pk: int) -> None:
        from . import traversal

        try:
            root, children = traversal.call_tree(pk, max_depth=self.max_depth)
            process_pks = [pk] + [
                ref.pk for refs in children.values() for ref in refs
            ]
            data = (
                traversal.data_links(process_pks) if self.show_data_nodes else {}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Building the call tree for %s failed", pk)
            self.call_from_thread(
                self._render_workflow_placeholder, f"Could not read {pk}: {exc}"
            )
            return
        self.call_from_thread(self._populate_workflow_tree, pk, root, children, data)

    def _populate_workflow_tree(
        self, pk: int, root, children: dict, data: dict | None = None
    ) -> None:
        if pk != self._workflow_pk:
            return  # the cursor moved on while we were querying
        if root is None:
            self._render_workflow_placeholder(f"{pk} is not a process node")
            return

        data = data or {}
        total = sum(len(v) for v in children.values())
        failed = sum(1 for refs in children.values() for r in refs if r.failed)
        header = f"[b]Call graph[/b] · {total} called, {failed} failed"
        if self.show_data_nodes:
            n_data = sum(len(v) for v in data.values())
            header += f" · {n_data} data"
        self.query_one("#workflow_header", Static).update(
            header + "\nEnter to open · D data nodes · w to hide"
        )

        tree = self.workflow_tree
        tree.reset(self._workflow_node_label(root))
        tree.root.data = root
        self._add_workflow_children(tree.root, root.pk, children, data)
        self._attach_data_branches(tree.root, root.pk, data)
        # Expand the process spine but leave the data branches folded, so the
        # call graph stays as legible as `verdi process status`.
        self._expand_processes(tree.root)

    def _add_workflow_children(
        self, parent: TreeNode, parent_pk: int, children: dict, data: dict
    ) -> None:
        for ref in children.get(parent_pk, ()):
            grandchildren = children.get(ref.pk)
            label = self._workflow_node_label(ref)
            has_data = self.show_data_nodes and data.get(ref.pk)
            if grandchildren or has_data:
                node = parent.add(label, data=ref)
                self._add_workflow_children(node, ref.pk, children, data)
                self._attach_data_branches(node, ref.pk, data)
            else:
                parent.add_leaf(label, data=ref)

    def _attach_data_branches(self, parent: TreeNode, pk: int, data: dict) -> None:
        """Hang inputs and outputs off a process as two collapsed branches."""
        if not self.show_data_nodes:
            return
        refs = data.get(pk) or []
        if not refs:
            return
        for direction, colour in (("input", "cyan"), ("output", "green")):
            group = [r for r in refs if r.direction == direction]
            if not group:
                continue
            heading = Text()
            heading.append(f"{direction}s", style=f"bold {colour}")
            heading.append(f" ({len(group)})", style="dim")
            branch = parent.add(heading, data=None)
            branch.collapse()
            for ref in group:
                branch.add_leaf(self._data_node_label(ref), data=ref)

    def _expand_processes(self, node: TreeNode) -> None:
        """Expand process nodes only; leave inputs/outputs folded."""
        node.expand()
        for child in node.children:
            ref = child.data
            if ref is not None and hasattr(ref, "is_calcjob"):
                self._expand_processes(child)

    @staticmethod
    def _data_node_label(ref) -> Text:
        """`link_label: StructureData<123> Ca2N`."""
        text = Text()
        if ref.link_label:
            text.append(f"{ref.link_label}", style="italic")
            text.append(": ", style="dim")
        style = "bold magenta" if ref.is_structure else "bold"
        text.append(ref.kind, style=style)
        text.append(f"<{ref.pk}>", style="dim")
        summary = ref.summary()
        if summary:
            text.append(f"  {summary}", style="yellow" if ref.is_structure else "dim")
        return text

    @staticmethod
    def _workflow_node_label(ref) -> Text:
        """Colour a `verdi process status` line by outcome."""
        text = Text()
        text.append(f"{ref.label}", style="bold")
        text.append(f"<{ref.pk}>", style="dim")
        if ref.call_link and ref.call_link not in ("CALL", "CALL_CALC", "CALL_WORK"):
            text.append(f" | {ref.call_link}", style="dim italic")

        state = (ref.process_state or "none").lower()
        style = STATE_STYLES.get(state, "")
        text.append(f"  {state.capitalize()}", style=style)

        if ref.exit_status:
            text.append(f" [{ref.exit_status}]", style="bold yellow")
        elif ref.exit_status == 0:
            text.append(" [0]", style="dim")

        if ref.stepper_state_info:
            text.append(f"  {ref.stepper_state_info}", style="dim italic")
        if ref.exit_message:
            text.append(f"  — {ref.exit_message}", style="italic red")
        return text

    def on_tree_node_selected(self, event) -> None:
        """Open whatever was picked: a process navigates, a data node inspects."""
        ref = event.node.data
        if ref is None:
            return  # an "inputs"/"outputs" heading — let it expand normally
        event.stop()
        if hasattr(ref, "is_calcjob"):
            self._open_process(ref.pk, ref.is_calcjob)
        else:
            self.inspect_data_node(ref.pk)

    def on_tree_node_highlighted(self, event) -> None:
        """Describe the highlighted node in the panel header."""
        ref = getattr(event.node, "data", None)
        if ref is None:
            return
        if not hasattr(ref, "is_calcjob"):
            label = f"{ref.kind}<{ref.pk}>"
            if ref.link_label:
                label = f"{ref.link_label}: {label}"
            summary = ref.summary()
            try:
                self.query_one("#workflow_header", Static).update(
                    f"{label}\n{summary}" if summary else label
                )
            except Exception:  # noqa: BLE001
                pass
            return
        detail = ref.status_line()
        if ref.process_status:
            detail += f"\n{ref.process_status}"
        elif ref.exit_message:
            detail += f"\n{ref.exit_message}"
        try:
            self.query_one("#workflow_header", Static).update(detail)
        except Exception:  # noqa: BLE001
            pass

    def _open_process(self, pk: int, is_calcjob: bool) -> None:
        """Navigate the main pane to a process chosen outside the table."""
        try:
            node = orm.load_node(pk)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Could not load {pk}: {exc}", severity="error")
            return

        if self.detail_view is not None:
            self.detail_view.display = False
        if self.table is not None:
            self.table.display = True

        previous_mode, previous_node = self.mode, self.current_node
        self.current_node = node

        if is_calcjob:
            if not self.show_file_list(node):
                self.current_node = previous_node
                return
        else:
            self.show_descendants(node)

        if previous_mode in ("nodes", "descendants", "file_list", "file_view"):
            self.navigation_stack.append(
                (previous_mode if previous_mode != "file_view" else "file_list", previous_node)
            )
        if self.table is not None:
            self.table.focus()

    def action_inspect_data(self) -> None:
        """Inspect the data node under the cursor."""
        pk = self._cursor_pk()
        if pk is None:
            self.notify("No row selected")
            return
        self.inspect_data_node(pk)

    def inspect_data_node(self, pk: int) -> None:
        self._inspect_data_worker(pk)

    @work(thread=True, exclusive=True, group="summary")
    def _inspect_data_worker(self, pk: int) -> None:
        from . import datainfo

        try:
            node = orm.load_node(pk)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.notify, f"Could not load {pk}: {exc}", severity="error"
            )
            return

        if isinstance(node, orm.ProcessNode):
            # Wrong entry point for a process; show its failure summary instead.
            self.call_from_thread(self._build_failure_summary, pk)
            return

        body = datainfo.describe(node)
        title = f"{type(node).__name__} {pk}"
        try:
            formula = node.get_formula()
            title = f"{formula} — {type(node).__name__} {pk}"
        except Exception:  # noqa: BLE001
            pass
        self.call_from_thread(self._show_text_panel, title, body)

    def action_toggle_data_nodes(self) -> None:
        """Show or hide inputs/outputs in the workflow tree."""
        self.show_data_nodes = not self.show_data_nodes
        self.save_settings()
        self.notify(
            "Provenance: data nodes shown"
            if self.show_data_nodes
            else "Provenance: processes only"
        )
        self.refresh_workflow_panel(force=True)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_cursor_top(self) -> None:
        if self.table is not None and self.table.row_count:
            self.table.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        if self.table is not None and self.table.row_count:
            self.table.move_cursor(row=self.table.row_count - 1)

    def _cursor_pk(self) -> int | None:
        """PK of the row under the cursor, or None."""
        if self.table is None or not self.table.row_count:
            return None
        cursor = self.table.cursor_row
        if cursor is None or cursor >= self.table.row_count:
            return None
        try:
            return int(self.table.get_row_at(cursor)[0])
        except (TypeError, ValueError, IndexError):
            return None

    def action_failure_summary(self) -> None:
        """Explain a failure without drilling into it.

        Everything here is already in the database or one file read away, but
        previously required navigating workchain -> child -> CalcJob -> file.
        """
        pk = self._cursor_pk()
        if pk is None:
            self.notify("No row selected")
            return
        self._build_failure_summary(pk)

    @work(thread=True, exclusive=True, group="summary")
    def _build_failure_summary(self, pk: int) -> None:
        from . import traversal

        try:
            node = orm.load_node(pk)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Could not load {pk}: {exc}", severity="error")
            return

        lines: list[str] = []
        add = lines.append
        add(f"PK {node.pk}   UUID {node.uuid}")
        label = getattr(node, "process_label", None) or node.node_type
        add(f"Process    {label}")
        state = getattr(node, "process_state", None)
        add(f"State      {getattr(state, 'value', state)}")
        exit_status = getattr(node, "exit_status", None)
        exit_message = getattr(node, "exit_message", None)
        add(f"Exit       {exit_status if exit_status is not None else '—'}"
            + (f"  ({exit_message})" if exit_message else ""))
        add(f"Created    {node.ctime:%Y-%m-%d %H:%M:%S}")
        add(f"Modified   {node.mtime:%Y-%m-%d %H:%M:%S}")
        for tag in sorted(self.tags.get(pk, ())):
            add(f"Tag        {tag}")

        exception = None
        try:
            exception = node.base.attributes.get("exception", None)
        except Exception:  # noqa: BLE001
            pass
        if exception:
            add("")
            add("Exception")
            add("-" * 72)
            add(str(exception).strip())

        # The failing call chain.
        try:
            forest = traversal.call_forest([pk])
            refs = forest.get(pk, [])
        except Exception as exc:  # noqa: BLE001
            refs = []
            add(f"\n[could not walk the call graph: {exc}]")

        failing = [r for r in refs if r.failed]
        add("")
        add(f"Called processes: {len(refs)} total, {len(failing)} failed")
        add("-" * 72)
        for ref in sorted(failing, key=lambda r: (r.depth, r.pk))[:20]:
            indent = "  " * ref.depth
            kind = "CalcJob" if ref.is_calcjob else "WorkChain"
            add(
                f"{indent}{kind} {ref.pk}  {ref.label}  "
                f"{ref.process_state or '—'}"
                + (f"  exit {ref.exit_status}" if ref.exit_status else "")
            )

        candidates = traversal.select_candidate_calcjobs(refs, limit=1)
        if candidates:
            calc_pk = candidates[0].pk
            nodes = traversal.load_calcjobs([calc_pk])
            calc = nodes.get(calc_pk)
            if calc is not None:
                for filename in ("_scheduler-stderr.txt", "CRASH", "aiida.out"):
                    preview = node_inspector.read_preview(
                        calc, filename, "output", head=0, tail=25
                    )
                    if preview.tail and not preview.tail[0].startswith("[file not found"):
                        add("")
                        add(f"Tail of {filename} (CalcJob {calc_pk})")
                        add("-" * 72)
                        lines.extend(preview.tail)
                        break

        self.call_from_thread(self._show_text_panel, f"Why did {pk} fail?", "\n".join(lines))

    def _show_text_panel(self, title: str, body: str) -> None:
        """Render read-only text in the detail pane."""
        assert self.detail_view is not None and self.table is not None
        self._panel_return_mode = self.mode
        self.mode = "panel"
        self.table.display = False
        self.detail_view.display = True
        self.detail_view.text = f"{title}\n{'=' * 72}\n{body}\n"
        self._set_title(f"{title} — 'b' or Escape to go back")
        self.detail_view.focus()

    def action_statistics(self) -> None:
        """What is actually killing this campaign?"""
        if not self.group:
            self.notify("Open a group first", severity="warning")
            return
        self._build_statistics(self.group.label)

    @work(thread=True, exclusive=True, group="summary")
    def _build_statistics(self, group_label: str) -> None:
        from collections import Counter

        from . import traversal

        try:
            fathers = traversal.failed_workchains_in_group(group_label)
            forest = traversal.call_forest([f.pk for f in fathers])
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Could not read group: {exc}", severity="error")
            return

        total = len(fathers)
        tagged = sum(1 for f in fathers if self.tags.get(f.pk))
        by_state: Counter = Counter(f.process_state or "unknown" for f in fathers)
        by_exit: Counter = Counter()
        no_calcjob = 0
        for father in fathers:
            calcs = [r for r in forest.get(father.pk, []) if r.is_calcjob and r.failed]
            if not calcs:
                no_calcjob += 1
            for ref in calcs:
                if ref.exit_status:
                    by_exit[ref.exit_status] += 1

        tag_counts = storage.tag_counts(
            {pk: tags for pk, tags in self.tags.items() if pk in {f.pk for f in fathers}}
        )

        lines = [
            f"Group        {group_label}",
            f"Failed       {total}",
            f"Classified   {tagged}  ({100 * tagged // total if total else 0}%)",
            f"Unclassified {total - tagged}   <- the actionable set",
            f"No failing CalcJob found: {no_calcjob}",
            "",
            "By process state",
            "-" * 60,
        ]
        for state, count in by_state.most_common():
            lines.append(f"  {state:<24} {count:>5}")
        lines += ["", "By CalcJob exit code", "-" * 60]
        for code, count in by_exit.most_common(15):
            lines.append(f"  {code:<24} {count:>5}")
        lines += ["", "By tag", "-" * 60]
        if tag_counts:
            for tag, count in sorted(tag_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {tag:<24} {count:>5}")
        else:
            lines.append("  (nothing tagged yet)")

        self.call_from_thread(self._show_text_panel, f"Statistics: {group_label}", "\n".join(lines))

    def action_tag_inspector(self) -> None:
        """Read-only modal listing every tag, its count, and the rule behind it."""
        self.push_screen(
            TagInspectorScreen(storage.tag_counts(self.tags), list(self.classifiers))
        )

    def action_export_tagged(self) -> None:
        """Write the current view's tags — and the unclassified PKs — to disk.

        Three files: a readable .txt, a .csv for spreadsheets, and a .json for
        scripts. The unclassified list is included because that, not the tagged
        set, is what still needs attention.
        """
        if self.mode not in ("nodes", "descendants"):
            self.notify("Export only works from node lists", severity="warning")
            return
        if not self.nodes_list:
            self.notify("Nothing in the current view to export", severity="warning")
            return

        tagged_pks = [pk for pk in self.nodes_list if self.tags.get(pk)]
        untagged_pks = [pk for pk in self.nodes_list if not self.tags.get(pk)]

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = self.data_dir / f"export_{timestamp}"

        by_tag: dict[str, list[int]] = {}
        for pk in tagged_pks:
            for tag_name in self.tags[pk]:
                by_tag.setdefault(tag_name, []).append(pk)

        group_label = self.group.label if self.group is not None else "-"

        # --- human-readable -------------------------------------------------
        lines = [
            f"# Exported {datetime.datetime.now().isoformat(timespec='seconds')}",
            f"# Group: {group_label}",
            f"# View: {self.mode} (tag filter: {self._tag_filter})",
            f"# In view: {len(self.nodes_list)}   tagged: {len(tagged_pks)}   "
            f"unclassified: {len(untagged_pks)}",
            "",
        ]
        for tag_name in sorted(by_tag):
            pks = sorted(by_tag[tag_name])
            lines.append(f"[{tag_name}] ({len(pks)})")
            lines.extend(str(pk) for pk in pks)
            lines.append("")
        if untagged_pks:
            lines.append(f"[UNCLASSIFIED] ({len(untagged_pks)})")
            lines.extend(str(pk) for pk in sorted(untagged_pks))
            lines.append("")
            lines.append("# Inspect these with:")
            lines.append(
                "#   verdi process list -a -p1 -P pk state exit_status "
                + " ".join(str(pk) for pk in sorted(untagged_pks)[:20])
                + (" ..." if len(untagged_pks) > 20 else "")
            )

        # --- csv ------------------------------------------------------------
        csv_lines = ["pk,tags"]
        for pk in self.nodes_list:
            tags = "|".join(sorted(self.tags.get(pk, ())))
            csv_lines.append(f"{pk},{tags}")

        # --- json -----------------------------------------------------------
        payload = {
            "exported": datetime.datetime.now().isoformat(timespec="seconds"),
            "group": group_label,
            "mode": self.mode,
            "tag_filter": self._tag_filter,
            "counts": {
                "in_view": len(self.nodes_list),
                "tagged": len(tagged_pks),
                "unclassified": len(untagged_pks),
            },
            "by_tag": {name: sorted(pks) for name, pks in sorted(by_tag.items())},
            "unclassified": sorted(untagged_pks),
            "classifiers": dump_classifiers(self.classifiers),
        }

        written = []
        try:
            txt = stem.with_suffix(".txt")
            txt.write_text("\n".join(lines))
            written.append(txt.name)
            csv = stem.with_suffix(".csv")
            csv.write_text("\n".join(csv_lines) + "\n")
            written.append(csv.name)
            storage.atomic_write_json(stem.with_suffix(".json"), payload)
            written.append(stem.with_suffix(".json").name)
        except OSError as exc:
            self.notify(f"Export failed: {exc}", severity="error")
            return

        self.notify(
            f"Exported {len(tagged_pks)} tagged / {len(untagged_pks)} unclassified "
            f"→ {', '.join(written)}",
            timeout=8,
        )

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
        """Escape: close search, dismiss a panel, or cancel a running scan."""
        if event.key == "escape" and self.mode == "panel":
            event.prevent_default()
            self.action_go_back()
            return

        if event.key == "escape" and self.workers and not self._search_active:
            running = [w for w in self.workers if w.group == "scan" and w.is_running]
            if running:
                event.prevent_default()
                self.action_cancel_scan()
                return

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

        # Tags only ever exist on group-member workchains, so the tag filter
        # applies to the nodes list alone.
        tag_filter_active = self._tag_filter != "all" and self.mode == "nodes"
        if tag_filter_active:
            matching_rows = [
                row for row in matching_rows if self._row_matches_tag_filter(row)
            ]

        # First column is the PK in the node lists.
        if self.mode in ("nodes", "descendants"):
            new_nodes_list = []
            for row in matching_rows:
                try:
                    new_nodes_list.append(int(row[0]))
                except (ValueError, IndexError):
                    pass
            self.nodes_list = new_nodes_list

        with self.batch_update():
            self.table.clear()
            if matching_rows:
                self.table.add_rows(matching_rows)

        # Reflect filter + search state in the title (without mutating the base title).
        if self.title_widget is not None:
            suffix_parts = []
            if tag_filter_active:
                label = "Tagged only" if self._tag_filter == "tagged" else "Untagged only"
                if not matching_rows:
                    # Never let a filter empty the table with no explanation.
                    suffix_parts.append(
                        f"[b red]{label}: 0 of {len(self._all_table_rows)} rows "
                        f"— press T to cycle the filter[/b red]"
                    )
                else:
                    suffix_parts.append(
                        f"[b]{label}[/b] ({len(matching_rows)}/{len(self._all_table_rows)})"
                    )
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
