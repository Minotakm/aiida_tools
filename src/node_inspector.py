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
