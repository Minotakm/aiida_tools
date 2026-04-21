"""Functions to inspect node details and extract error information."""

from __future__ import annotations

from aiida.orm import CalcJobNode


def get_retrieved_files(node: CalcJobNode) -> list[str]:
    """List all files in the retrieved folder.

    Returns list of filenames, or empty list if not a CalcJob
    """
    try:
        if hasattr(node, "outputs") and hasattr(node.outputs, "retrieved"):
            return node.outputs.retrieved.list_object_names()
    except Exception:
        pass
    return []


def get_retrieved_file_size(node: CalcJobNode, filename: str) -> int | None:
    """Return size in bytes of a retrieved file, or None if unknown."""
    try:
        with node.outputs.retrieved.open(filename, "rb") as f:
            f.seek(0, 2)
            return f.tell()
    except Exception:
        try:
            content = node.outputs.retrieved.get_object_content(filename, mode="rb")
            return len(content)
        except Exception:
            return None


def get_input_file_size(node: CalcJobNode, filename: str) -> int | None:
    """Return size in bytes of an input file, or None if unknown."""
    try:
        with node.base.repository.open(filename, "rb") as f:
            f.seek(0, 2)
            return f.tell()
    except Exception:
        try:
            content = node.base.repository.get_object_content(filename, mode="rb")
            return len(content)
        except Exception:
            return None


def get_file_content(
    node: CalcJobNode, filename: str, head_lines: int = 50, tail_lines: int = 50
) -> str:
    """Get content of a retrieved file (first N + last N lines).

    Args:
        node: The calculation node
        filename: Name of file to retrieve
        head_lines: Number of lines from start
        tail_lines: Number of lines from end

    Returns formatted string with file content
    """
    try:
        if not hasattr(node, "outputs") or not hasattr(node.outputs, "retrieved"):
            return "[No retrieved folder found]"

        content = node.outputs.retrieved.get_object_content(filename)
        lines = content.splitlines()

        total_lines = len(lines)

        if head_lines == 0:
            # Only show tail
            return "\n".join(lines[-tail_lines:])

        if total_lines <= (head_lines + tail_lines):
            # File is small enough to show entirely
            return content

        # Show head and tail with separator
        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        separator = (
            f"\n... [{total_lines - head_lines - tail_lines} lines omitted] ...\n"
        )

        return head + separator + tail

    except Exception as e:
        return f"[Error reading file: {e}]"


def get_input_files(node: CalcJobNode) -> list[str]:
    """List available input files from the repository folder.

    Returns list of filenames that are commonly useful.
    """
    available_files = []

    try:
        # Check for raw input folder
        if hasattr(node, "base") and hasattr(node.base, "repository"):
            repo_files = node.base.repository.list_object_names()
            # Common input files
            common_inputs = [
                "aiida.in",
                "_aiidasubmit.sh",
                ".aiida/job_tmpl.json",
                ".aiida/calcinfo.json",
            ]
            available_files = [f for f in common_inputs if f in repo_files]
    except Exception:
        pass

    return available_files


def get_input_file_content(node: CalcJobNode, filename: str) -> str:
    """Get content of an input file from the repository.

    Args:
        node: The calculation node
        filename: Name of file to retrieve

    Returns formatted string with file content
    """
    try:
        if hasattr(node, "base") and hasattr(node.base, "repository"):
            content = node.base.repository.get_object_content(filename)
            return content
        return "[Repository not accessible]"
    except Exception as e:
        return f"[Error reading file: {e}]"
