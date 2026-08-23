"""The bot's avatar: a sibling of the MusicLore scorpion badge.

The first two attempts failed because they invented a visual language --
thin cyan outlines, glows, gradients -- while the MusicLore icon is flat,
chunky, strokeless shapes in a limited palette inside a ring. Matching the
family meant copying the GRAMMAR, not the subject:

    circular badge, bold ring, near-black field
    top-down scorpion, head up, claws forward at the bottom
    solid fills, no outlines, no gradients, no glow
    chevron legs, a curled tail cluster behind the head

The "coder" reading comes from the silhouette rather than decoration:
the claws are square brackets, and the carapace carries a terminal prompt.
A cyan ring instead of MusicLore's red says same family, different app.

    python tools/make_scorpion.py > scorpion.svg
"""

from __future__ import annotations

S = 512
CX = 256.0

FIELD = "#111318"        # near-black badge interior
RING = "#3ad0c8"         # cyan-green: the sibling of MusicLore's red
SHELL = "#46596b"        # carapace
SHELL_DK = "#33424f"     # tail cluster, shadowed parts
FACE = "#8aa0b2"         # lighter face plate, as in the reference
EYE = "#14171c"
PROMPT = "#7ee787"       # terminal green
LIMB = "#2b3742"


def badge() -> list[str]:
    return [
        f'<circle cx="{CX}" cy="{CX}" r="248" fill="{FIELD}"/>',
        f'<circle cx="{CX}" cy="{CX}" r="228" fill="none" stroke="{RING}" '
        f'stroke-width="22"/>',
    ]


def legs() -> list[str]:
    """Chevrons either side, as in the reference: thin, angular, no joints."""
    out = []
    for i, y in enumerate((214.0, 254.0, 294.0)):
        reach = 74 - i * 4
        for side in (-1, 1):
            x0 = CX + side * 60
            x1 = x0 + side * reach * 0.55
            x2 = x1 + side * reach * 0.45
            d = (f"M{x0:.0f},{y - 16:.0f} L{x1:.0f},{y:.0f} "
                 f"L{x2:.0f},{y - 18:.0f}")
            out.append(f'<path d="{d}" fill="none" stroke="{LIMB}" '
                       f'stroke-width="13" stroke-linecap="round" '
                       f'stroke-linejoin="round"/>')
    return out


def tail() -> list[str]:
    """A curled cluster behind the head, matching the reference's blob,
    but ordered so it still reads as segments tapering to a stinger."""
    out = []
    blobs = [(300.0, 150.0, 26.0), (286.0, 118.0, 23.0), (262.0, 100.0, 20.0),
             (234.0, 96.0, 17.0), (210.0, 104.0, 14.0)]
    for x, y, r in blobs:
        out.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{SHELL_DK}"/>')
    # Stinger: a small solid wedge, no glow -- the reference has no glow.
    # Chunky enough to read at avatar size, and overlapping the last
    # segment so it is part of the tail rather than floating beside it.
    out.append(f'<path d="M216,114 L206,86 L172,98 Z" fill="{RING}"/>')
    return out


def claws() -> list[str]:
    """Chunky bottom claws, shaped as square brackets.

    The reference's claws are already blocky rounded rectangles with a
    notch; making the notch read as `[` and `]` costs nothing and is the
    whole 'coder' idea in one shape.
    """
    out = []
    for side in (-1, 1):
        x = CX + side * 118
        # Outer bar of the bracket
        out.append(f'<rect x="{x - 30:.0f}" y="300" width="60" height="118" '
                   f'rx="18" fill="{SHELL}"/>')
        # The notch: a field-coloured cut that turns the block into a bracket,
        # opening toward the centre so left reads "[" and right reads "]".
        nx = x - 30 + (34 if side < 0 else 0)
        out.append(f'<rect x="{nx:.0f}" y="326" width="34" height="66" '
                   f'rx="10" fill="{FIELD}"/>')
        # Arm connecting the claw up to the body.
        out.append(f'<path d="M{x - side * 6:.0f},308 L{CX + side * 54:.0f},288" '
                   f'fill="none" stroke="{LIMB}" stroke-width="20" '
                   f'stroke-linecap="round"/>')
    return out


def body() -> list[str]:
    out = []
    # Carapace: a teardrop, wide at the bottom, rounded at the top.
    out.append(
        f'<path d="M{CX},128 '
        f'C{CX + 46},128 {CX + 74},176 {CX + 74},232 '
        f'L{CX + 74},288 C{CX + 74},322 {CX + 40},340 {CX},340 '
        f'C{CX - 40},340 {CX - 74},322 {CX - 74},288 '
        f'L{CX - 74},232 C{CX - 74},176 {CX - 46},128 {CX},128 Z" '
        f'fill="{SHELL}"/>'
    )
    # Face plate, as in the reference: a lighter rounded block low on the body.
    out.append(f'<rect x="{CX - 62:.0f}" y="268" width="124" height="86" '
               f'rx="34" fill="{FACE}"/>')
    out.append(f'<circle cx="{CX - 26:.0f}" cy="302" r="13" fill="{EYE}"/>')
    out.append(f'<circle cx="{CX + 26:.0f}" cy="302" r="13" fill="{EYE}"/>')
    # Terminal prompt on the carapace: the coder tell, in the icon's own
    # flat language -- solid strokes, no outline, no glow.
    out.append(f'<path d="M{CX - 34:.0f},186 L{CX - 10:.0f},206 '
               f'L{CX - 34:.0f},226" fill="none" stroke="{PROMPT}" '
               f'stroke-width="14" stroke-linecap="round" '
               f'stroke-linejoin="round"/>')
    out.append(f'<rect x="{CX + 2:.0f}" y="216" width="42" height="13" '
               f'rx="6" fill="{PROMPT}"/>')
    return out


def svg() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" '
        f'width="{S}" height="{S}">'
    ]
    parts += badge()
    parts += legs()
    parts += tail()
    parts += claws()
    parts += body()
    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    print(svg())
