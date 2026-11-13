"""Constants that both halves of the main window read: main.py (the on/off logic) and main_view.py (the drawing).

Moved unchanged out of main.py, which re-exports every name. Plain values only: no imports.
"""

# Logical layout heights. BASE: status line + button (detail hidden). Panels below: the memory card (MEM_CARD_H) and
# the graph card (82 px per row + 1 px hairlines), then the expand bar (BAR_BLOCK) or, with everything pinned, a 16 px
# margin. HEIGHT = the default look: memory pinned, collapsed.
BASE_H = 110
MEM_CARD_H = 73
BAR_BLOCK = 24                           # BAR_PAD + BAR_H + BAR_PAD
WIDTH, HEIGHT = 360, BASE_H + 14 + MEM_CARD_H + BAR_BLOCK
STATS_H = 3 * 82 + 2                     # three StatRows + two hairlines (logical px)
PANELS = ("memory", "vram", "decode", "prompt")   # top to bottom; pinned ones stay visible while collapsed
STATS_GAP = 8                            # between the memory card and the stats card when expanded
HEIGHT_EXPANDED = HEIGHT + STATS_GAP + STATS_H

IDLE, STARTING, ACTIVE, STOPPING, ERROR = "IDLE", "STARTING", "ACTIVE", "STOPPING", "ERROR"

STATS_MS = 2000                          # stats redraw + /metrics poll period
