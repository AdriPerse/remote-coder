"""Generate the CLI banner scorpion from a pixel grid.

Half-block rendering: each character cell encodes TWO vertical pixels --
fg color = top pixel, bg color = bottom pixel -- using U+2580 UPPER HALF
BLOCK. Where both pixels are identical a full block or a space does.

Palette (ANSI 256):
  T teal    = #3ad0c8 (the badge ring)   -> 80
  S slate   = #46596b (the body)         -> 66
  L light   = #8aa0b2 (the face panel)   -> 103
  G green   = #7ee787 (the eyes)         -> 120
  D dark    = #33424f (tail segments)    -> 59
  . transparent

Keep the grid 40 wide. Run: python tools/gen_scorpion.py --write
to regenerate BANNER in cli.py.
"""
import re
import sys
from pathlib import Path

GRID = [
    "................................................",
    "................................................",
    "................................................",
    "....................SSSSTTTTT...................",
    "...................SS.....TTTT..................",
    ".........................TTTT...................",
    "...................BBBBBBTTT....................",
    "..................BBBBBBBT......................",
    ".................BBBBBBBBB......................",
    ".........LL...L.BB..>.BBBBB.L...LL..............",
    "..........LL..BBB..>.>__BBB.LL..................",
    "...........LL..LBBBBBBBBBBBBL..LL...............",
    "............LL..BBBBBBBBBBBB..LL................",
    ".............LL..BB......BB..LL.................",
    "..............LLLB..EE..EE..BLLL................",
    "...............CCCC.B..EE..EE..B.CCCC...........",
    "..............CCC..............CCC..............",
    "..............CCC..............CCC..............",
    "..............CCC..............CCC..............",
    "..............CCC..C........C..CCC..............",
    "...............CCC............CCC...............",
    "................................................",
    "................................................",
    "................................................",
    "................................................",
    "................................................",
    "................................................",
    "................................................",
]

# ANSI 256-palette: S stinger, T tail, B body, L legs, E eyes, C claws.
COLORS = {"S": 120, "T": 60, "B": 103, "L": 66, "E": 120, "C": 66,
          ">": 60, "_": 60}
UPPER = "▀"   # fg = top pixel, bg = bottom pixel
FULL = "█"


def pairs(grid):
    """Yield (row_pairs) -> list of (top_char, bottom_char) per column."""
    for i in range(0, len(grid) - 1, 2):
        yield list(zip(grid[i], grid[i + 1]))


def render_ansi(grid):
    lines = []
    for pair_row in pairs(grid):
        out = []
        for top, bottom in pair_row:
            if top == "." and bottom == ".":
                out.append("\x1b[0m ")
            elif top == bottom:
                out.append("\x1b[38;5;{}m{}\x1b[0m".format(COLORS[top], FULL))
            elif bottom == ".":
                out.append(
                    "\x1b[38;5;{}m{}\x1b[0m".format(COLORS[top], UPPER))
            elif top == ".":
                out.append(
                    "\x1b[38;5;{}m▄\x1b[0m".format(COLORS[bottom]))
            else:
                out.append(
                    "\x1b[38;5;{}m\x1b[48;5;{}m{}\x1b[0m".format(
                        COLORS[top], COLORS[bottom], UPPER))
        lines.append("".join(out).rstrip())
    return "\n".join(lines)


def render_mono(grid):
    """Character art: keep the glyphs, blank the dots."""
    return "\n".join(
        "".join(" " if c == "." else c for c in row).rstrip() for row in grid
    )


def render_char_ansi(grid):
    """Per-character color by region letter, preserving the hand-drawn art."""
    lines = []
    for row in grid:
        out = []
        for c in row:
            if c == ".":
                out.append("\x1b[0m ")
            else:
                out.append("\x1b[38;5;{}m{}\x1b[0m".format(COLORS.get(c, 103), c))
        lines.append("".join(out).rstrip())
    return "\n".join(lines)


def main():
    mono = render_mono(GRID)
    # Emit as JSON so a cp1252 console cannot mangle it: the caller reads
    # and writes the file, the console never touches the glyphs.
    import json
    print(json.dumps({"mono": mono, "ansi": render_char_ansi(GRID)}))
    if "--write" not in sys.argv:
        return
    path = Path(__file__).resolve().parent.parent / "cli.py"
    text = path.read_text(encoding="utf-8")
    ansi = render_char_ansi(GRID)
    banner = 'BANNER = """\n' + ansi + '\n\n      R E M O T E   C O D E R\n"""'
    new = re.sub(r'BANNER = """.*?"""', banner, text, count=1, flags=re.S)
    if new == text:
        sys.exit("BANNER block not found in cli.py")
    path.write_text(new, encoding="utf-8", newline="\n")
    print("\ncli.py updated (ANSI true-color version)")


if __name__ == "__main__":
    main()
