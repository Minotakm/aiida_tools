# AiiDA Groups and Nodes TUI

A Terminal User Interface (TUI) for browsing AiiDA groups, nodes, and inspecting calculation outputs.

## Features

- Browse AiiDA groups
- View nodes within groups
- Navigate through process descendants
- View output files (aiida.out, scheduler outputs)
- Adjust preview lines dynamically

## Requirements

```bash
pip install aiida-core textual
```

## Usage

```bash
# Start TUI and browse all groups
python src/main.py

# Start TUI with a specific group
python src/main.py "my-group-label"
python src/main.py 123  # by PK
```

## Navigation

### Keybindings

| Key | Action | Description |
|-----|--------|-------------|
| `a` | Select | Select group/node and drill down |
| `v` | View Files | View output files of selected calculation |
| `b` | Back | Go back to previous view |
| `r` | Refresh | Reload current view |
| `+` | More lines | Increase preview lines (+20) |
| `-` | Fewer lines | Decrease preview lines (-20) |
| `q` | Quit | Exit the application |

### Workflow

1. **Groups View** - Browse all AiiDA core groups
2. **Nodes View** - Select a group (`a`) to see all nodes
3. **Descendants View** - Select a node (`a`) to see called processes
4. **Files View** - Press `v` on any CalcJob to view output files

## File Structure

```
src/
├── main.py              # Entry point
├── app.py               # Main TUI application
├── queries.py           # AiiDA database queries
└── node_inspector.py    # File inspection utilities
```

## Output Files Displayed

When viewing files (`v`), the TUI shows the **last N lines** (default: 50) of:
- `aiida.out` - Main calculation output
- `_scheduler-stdout.txt` - Scheduler standard output
- `_scheduler-stderr.txt` - Scheduler standard error

Use `+`/`-` to adjust how many lines are shown (increments of 20).
