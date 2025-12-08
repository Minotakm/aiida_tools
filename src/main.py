#!/usr/bin/env python
"""Entry point for the AiiDA Groups and Nodes TUI.

Usage:
    python main.py [GROUP_IDENTIFIER]

where GROUP_IDENTIFIER can be:
  - group label (e.g., "my-group")
  - group PK (e.g., 123)
  - group UUID (e.g., "a1b2c3d4-...")
"""

from __future__ import annotations

import sys

from aiida.manage.configuration import load_profile

from app import GroupNodesApp


def main() -> None:
    """Entry point for the TUI application."""
    load_profile()

    # Get group identifier from command line if provided
    group_identifier = sys.argv[1] if len(sys.argv) > 1 else None

    app = GroupNodesApp(group_identifier=group_identifier)
    app.run()


if __name__ == "__main__":
    main()
