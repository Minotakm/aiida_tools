#!/usr/bin/env python
"""Entry point for the AiiDA Error Inspector TUI.

Usage:
    aiida-error-inspector [GROUP_IDENTIFIER]

where GROUP_IDENTIFIER can be:
  - group label (e.g., "my-group")
  - group PK (e.g., 123)
  - group UUID (e.g., "a1b2c3d4-...")
"""

from __future__ import annotations

import sys

from aiida.manage.configuration import load_profile

from aiida_error_inspector.app import GroupNodesApp


def main() -> None:
    """Main entry point for the aiida-error-inspector CLI."""
    load_profile()

    group_identifier = sys.argv[1] if len(sys.argv) > 1 else None
    app = GroupNodesApp(group_identifier=group_identifier)
    app.run()


if __name__ == "__main__":
    main()
