"""Oracle — Interface Theme

Purpose:
    The fonts and colours of the architectural workspace. A restrained engineering palette: a cool neutral background, white surfaces, one
    steel-blue accent for the single primary action of a screen, and three status tones used sparingly and always with words (amber for
    "needs review", green for "accepted", muted red for "rejected"). Three voices are given distinct, quiet colours so an engineer can tell
    them apart at a glance: ORACLE SUGGESTS (amber), ENGINEER INPUT (blue) and ENGINEER DECISION (green).

Role in Oracle:
    Presentation constants only; no logic and no widgets. The wizard's own constants are unchanged; this module is for oracle.ui only.

Dependencies:
    None.

Consumers:
    oracle.ui.*

Status:
    Interface (interface refinement phase).

Migration/Notes:
    Names used by earlier interface code (BG, PANEL, ACCENT, MUTED, LINE, GREEN, AMBER, RED and their _BG tints, the font tuples) are kept.
"""

BG = "#f5f6f8"
PANEL = "#ffffff"
INK = "#1f2933"
MUTED = "#6b7683"
LINE = "#e2e5ea"
LINE_STRONG = "#b4bcc7"
ACCENT = "#2f5d9e"
ACCENT_HOVER = "#264c82"
ACCENT_SOFT = "#e9eff8"
HOVER = "#eef0f3"
GREEN = "#2f7a50"
GREEN_BG = "#eaf4ee"
AMBER = "#9a5b00"
AMBER_BG = "#fbf3e3"
RED = "#a63d3d"
RED_BG = "#f8ecec"

FONT = "Segoe UI"
DISPLAY = (FONT, 20, "bold")
TITLE = (FONT, 16, "bold")
H2 = (FONT, 13, "bold")
SUBTITLE = (FONT, 11)
BODY = (FONT, 10)
BOLD = (FONT, 10, "bold")
SMALL = (FONT, 9)
CAPTION = (FONT, 8, "bold")
MONO = ("Consolas", 9)

STATE_COLORS = {"ready": (GREEN, GREEN_BG), "review": (AMBER, AMBER_BG), "blocked": (RED, RED_BG)}
TONE = {"ok": GREEN, "attention": AMBER, "note": MUTED, "rejected": RED}
