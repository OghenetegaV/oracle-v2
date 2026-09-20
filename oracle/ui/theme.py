"""Oracle — Interface Theme

Purpose:
    The fonts and colours of the architectural workspace, taken from the existing Oracle wizard (Segoe UI, the same background, accent and
    muted grey) plus the few status colours a review screen needs: amber for proposed or unresolved, green for engineer-approved, red for
    blocked or rejected. One place, so the workspace looks like part of Oracle and stays restrained.

Role in Oracle:
    Presentation constants only; no logic and no widgets.

Dependencies:
    None.

Consumers:
    oracle.ui.architectural_workspace, oracle.ui.dialogs, oracle.ui.preview_canvas.

Status:
    Interface (interface phase).

Migration/Notes:
    The wizard's own constants (oracle_wizard.py) are duplicated here on purpose: the wizard is a root script that this package must not
    import. If they change, change both.
"""

BG = "#f4f6f8"
PANEL = "#ffffff"
ACCENT = "#1f6feb"
MUTED = "#6b7280"
LINE = "#c9d1d9"
GREEN = "#137333"
GREEN_BG = "#e6f4ea"
AMBER = "#92400e"
AMBER_BG = "#fef3c7"
RED = "#b91c1c"
RED_BG = "#fde8e8"

TITLE = ("Segoe UI", 16, "bold")
SUBTITLE = ("Segoe UI", 11)
BODY = ("Segoe UI", 10)
BOLD = ("Segoe UI", 10, "bold")
SMALL = ("Segoe UI", 9)
MONO = ("Consolas", 9)

STATE_COLORS = {"ready": (GREEN, GREEN_BG), "review": (AMBER, AMBER_BG), "blocked": (RED, RED_BG)}
