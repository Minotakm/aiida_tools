"""Reading files out of a CalcJob's retrieved folder and input repository.

Everything here streams. The previous implementation called
``get_object_content()``, which pulls a whole object into memory and decodes it,
and then threw away all but the last 2000 lines — once per node *per pattern*
during a scan. A 20 MB ``aiida.out`` cost 20 MB of reads to look at its tail.

Sizes are measured by a capped streaming count rather than
``get_object_size()``, which copies the object to a temporary path (see
``NodeRepository.get_object_size`` — it goes through ``as_path``).
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import Iterator, Literal, Sequence

logger = logging.getLogger(__name__)

FileType = Literal["output", "input"]

#: Shown first in the file list, in this order, when present. Everything else
#: the calculation retrieved is listed after them, sorted. ``CRASH`` is Quantum
#: ESPRESSO's canonical error file and was previously unreachable because the
#: list was hardcoded to three names.
PINNED_FILES: tuple[str, ...] = (
    "aiida.out",
    "CRASH",
    "_scheduler-stderr.txt",
    "_scheduler-stdout.txt",
    "aiida.in",
    "_aiidasubmit.sh",
)

DEFAULT_SIZE_CAP = 8 * 1024 * 1024
DEFAULT_READ_CAP = 16 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Repository access
# --------------------------------------------------------------------------- #


def _repository(node, file_type: FileType):
    """The repository holding this kind of file, or None if unavailable."""
    try:
        if file_type == "output":
            return node.outputs.retrieved.base.repository
        return node.base.repository
    except Exception:  # noqa: BLE001 - missing retrieved output is routine
        return None


def _walk_names(repository) -> list[str]:
    """Every file path in a repository, relative and recursive."""
    names: list[str] = []
    try:
        for root, _dirnames, filenames in repository.walk():
            for filename in filenames:
                path = str(root / filename) if str(root) != "." else filename
                names.append(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not walk repository: %s", exc)
    return names


def list_files(node, file_type: FileType) -> list[str]:
    """All files of one kind, pinned names first then the rest sorted.

    Previously the output list was hardcoded to three filenames, so anything
    else the calculation retrieved — ``CRASH`` above all — could not be opened.
    """
    repository = _repository(node, file_type)
    if repository is None:
        return []
    names = _walk_names(repository)
    pinned = [n for n in PINNED_FILES if n in names]
    rest = sorted(n for n in names if n not in pinned)
    return pinned + rest


def list_all_files(node) -> list[tuple[str, FileType]]:
    """(filename, kind) for every retrieved and input file, outputs first."""
    files: list[tuple[str, FileType]] = [(n, "output") for n in list_files(node, "output")]
    seen = {n for n, _ in files}
    files.extend((n, "input") for n in list_files(node, "input") if n not in seen)
    return files


@contextlib.contextmanager
def open_text(node, filename: str, file_type: FileType = "output") -> Iterator:
    """Text handle on a repository object, decoding errors replaced.

    QE output occasionally carries non-UTF8 bytes from the scheduler. Letting
    that raise used to turn into ``"[Error reading file: ...]"``, which then
    matched no pattern and was indistinguishable from "pattern absent".
    """
    repository = _repository(node, file_type)
    if repository is None:
        raise FileNotFoundError(f"no {file_type} repository on node {getattr(node, 'pk', '?')}")
    with repository.open(filename, "rb") as handle:
        import io

        yield io.TextIOWrapper(handle, encoding="utf-8", errors="replace")


def iter_lines(
    node,
    filename: str,
    file_type: FileType = "output",
    *,
    max_bytes: int | None = None,
) -> Iterator[str]:
    """Stream a file line by line, optionally stopping after ``max_bytes``."""
    consumed = 0
    with open_text(node, filename, file_type) as handle:
        for line in handle:
            line = line.rstrip("\n")
            if max_bytes is not None:
                consumed += len(line) + 1
                if consumed > max_bytes:
                    return
            yield line


def read_tail_lines(
    node, filename: str, file_type: FileType = "output", n: int = 500
) -> tuple[list[str], int]:
    """Last ``n`` lines, and the total line count.

    Uses a bounded deque over a forward pass. Seeking from the end is not an
    option: for objects in a compressed pack, ``seek(..., whence=2)`` either
    raises or silently forces a full decompress.
    """
    buffer: deque[str] = deque(maxlen=n)
    total = 0
    for line in iter_lines(node, filename, file_type):
        buffer.append(line)
        total += 1
    return list(buffer), total


@dataclass
class Preview:
    """A head/tail view of a file, with the middle elided."""

    head: list[str]
    tail: list[str]
    total_lines: int
    omitted: int
    truncated: bool = False

    def render(self) -> str:
        parts: list[str] = []
        if self.head:
            parts.append("\n".join(self.head))
        if self.omitted > 0:
            parts.append(f"\n... [{self.omitted} lines omitted] ...\n")
        if self.tail:
            parts.append("\n".join(self.tail))
        body = "\n".join(p for p in parts if p)
        if self.truncated:
            body += "\n\n[truncated — press 'f' to open the full file in a pager]"
        return body


def read_preview(
    node,
    filename: str,
    file_type: FileType = "output",
    *,
    head: int = 0,
    tail: int = 500,
    max_bytes: int = DEFAULT_READ_CAP,
) -> Preview:
    """One forward pass producing the first ``head`` and last ``tail`` lines."""
    head_lines: list[str] = []
    tail_buffer: deque[str] = deque(maxlen=tail) if tail else deque(maxlen=0)
    total = 0
    consumed = 0
    truncated = False

    try:
        with open_text(node, filename, file_type) as handle:
            for line in handle:
                line = line.rstrip("\n")
                consumed += len(line) + 1
                if consumed > max_bytes:
                    truncated = True
                    break
                if len(head_lines) < head:
                    head_lines.append(line)
                else:
                    tail_buffer.append(line)
                total += 1
    except FileNotFoundError:
        return Preview([], [f"[file not found: {filename}]"], 0, 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reading %s failed: %s", filename, exc)
        return Preview([], [f"[error reading file: {exc}]"], 0, 0)

    omitted = max(0, total - len(head_lines) - len(tail_buffer))
    return Preview(head_lines, list(tail_buffer), total, omitted, truncated)


def read_text(
    node,
    filename: str,
    file_type: FileType = "output",
    *,
    max_bytes: int = DEFAULT_READ_CAP,
) -> tuple[str, bool]:
    """Whole file as text, capped. Returns (text, truncated)."""
    chunks: list[str] = []
    consumed = 0
    truncated = False
    try:
        with open_text(node, filename, file_type) as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > max_bytes:
                    chunks.append(chunk[: max_bytes - (consumed - len(chunk))])
                    truncated = True
                    break
                chunks.append(chunk)
    except FileNotFoundError:
        return f"[file not found: {filename}]", False
    except Exception as exc:  # noqa: BLE001
        return f"[error reading file: {exc}]", False
    return "".join(chunks), truncated


def file_size(
    node, filename: str, file_type: FileType = "output", *, cap: int = DEFAULT_SIZE_CAP
) -> tuple[int | None, bool]:
    """Byte size by capped streaming. Returns (size, hit_cap).

    ``get_object_size()`` is not used: it copies the object to a temporary
    directory via ``as_path``, so displaying a size column could read hundreds
    of megabytes.
    """
    repository = _repository(node, file_type)
    if repository is None:
        return None, False
    total = 0
    try:
        with repository.open(filename, "rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    return cap, True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not size %s: %s", filename, exc)
        return None, False
    return total, False


def format_size(nbytes: int | None, hit_cap: bool = False) -> str:
    """Human-readable size, or '—' when unknown."""
    if nbytes is None:
        return "—"
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            rendered = f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
            return f">{rendered}" if hit_cap else rendered
        size /= 1024
    return f"{size:.1f} GB"


def search_file(
    node,
    filename: str,
    file_type: FileType,
    matchers: Sequence,
    *,
    max_bytes: int = DEFAULT_READ_CAP,
) -> set[str]:
    """Stream one file once, testing every matcher against it.

    Returns the set of matched tags. Stops as soon as every matcher has
    matched, so a hit near the top of a large file costs almost nothing. This
    is the primitive that lets a scan read each file once no matter how many
    classifiers target it.

    Empty-file matchers ride along: the first line with any content rules them
    out, and they match only if the whole file was read without finding one.
    """
    pending = [m for m in matchers if not getattr(m, "matches_empty", False)]
    empties = [m for m in matchers if getattr(m, "matches_empty", False)]
    matched: set[str] = set()
    if not pending and not empties:
        return matched

    consumed = 0
    try:
        with open_text(node, filename, file_type) as handle:
            for line in handle:
                consumed += len(line)
                if consumed > max_bytes:
                    empties = []
                    break
                if empties and line.strip():
                    empties = []
                still_pending = []
                for matcher in pending:
                    if matcher.matches_text(line):
                        matched.add(matcher.tag)
                    else:
                        still_pending.append(matcher)
                pending = still_pending
                if not pending and not empties:
                    break
        matched.update(m.tag for m in empties)
    except FileNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scanning %s on node %s failed: %s", filename, getattr(node, "pk", "?"), exc)
    return matched
