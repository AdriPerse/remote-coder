"""Draws the robot-scorpion mark for the bot, as SVG.

Generated rather than hand-written: the tail is a tapered curl of segments
marching along an arc, which is miserable to place by hand and trivial to
compute.

The lesson from the first attempt is baked in here: placing discrete
segments along a curve only reads as a TAIL if consecutive segments
overlap. Spacing greater than segment width gives a dotted line of debris.
So the tail is drawn twice -- a solid tapered spine first, segments on top
-- and continuity no longer depends on the spacing arithmetic working out.

    python tools/make_scorpion.py > scorpion.svg
"""

from __future__ import annotations

import math

W = H = 512
CX = 256.0

BG_OUTER = "#0a0e13"
BG_INNER = "#17222e"
PLATE = "#22323f"
PLATE_HI = "#31485c"
EDGE = "#4fd6e0"
EDGE_DIM = "#2f8792"
AMBER = "#ffb347"
AMBER_HOT = "#ffe0a8"

# The tail arc: starts below the body, sweeps up the right, curls over the
# head. SVG angles: 90 is straight down, 0 is right, -90 is straight up.
ARC_CX, ARC_CY, ARC_R = 262.0, 258.0, 132.0
ARC_START, ARC_END = 88.0, -98.0
TAIL_W0, TAIL_W1 = 34.0, 15.0


def arc_point(t: float) -> tuple[float, float, float]:
    """(x, y, angle_rad) at fraction t along the tail arc."""
    a = math.radians(ARC_START + (ARC_END - ARC_START) * t)
    return ARC_CX + ARC_R * math.cos(a), ARC_CY + ARC_R * math.sin(a), a


def hexagon(cx: float, cy: float, r: float, rot: float) -> str:
    pts = []
    for i in range(6):
        a = math.radians(rot + i * 60)
        pts.append(f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}")
    return " ".join(pts)


def tail() -> list[str]:
    out = []
    steps = 40
    left, right = [], []
    for i in range(steps + 1):
        t = i / steps
        x, y, a = arc_point(t)
        half = TAIL_W0 + (TAIL_W1 - TAIL_W0) * t
        nx, ny = math.cos(a), math.sin(a)          # radial normal
        left.append(f"{x + half * nx:.1f},{y + half * ny:.1f}")
        right.append(f"{x - half * nx:.1f},{y - half * ny:.1f}")
    # One solid tapered spine. Segment overlap is now cosmetic, not load-bearing.
    out.append(
        f'<polygon points="{" ".join(left + right[::-1])}" fill="{PLATE}" '
        f'stroke="{EDGE_DIM}" stroke-width="2.5" stroke-linejoin="round"/>'
    )
    # Segment plates on top.
    n = 9
    for i in range(n):
        t = i / (n - 1) * 0.94
        x, y, a = arc_point(t)
        half = TAIL_W0 + (TAIL_W1 - TAIL_W0) * t
        out.append(
            f'<polygon points="{hexagon(x, y, half * 0.86, math.degrees(a))}" '
            f'fill="{PLATE_HI}" stroke="{EDGE_DIM}" stroke-width="2"/>'
        )
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{half * 0.24:.1f}" '
            f'fill="{EDGE}" opacity="{0.3 + 0.55 * t:.2f}"/>'
        )
    # Stinger: continues along the tangent at the end of the arc.
    ex, ey, ea = arc_point(1.0)
    tangent = ea - math.pi / 2 if ARC_END < ARC_START else ea + math.pi / 2
    tipx, tipy = ex + 74 * math.cos(tangent), ey + 74 * math.sin(tangent)
    perp = tangent + math.pi / 2
    b1 = (ex + (TAIL_W1 + 4) * math.cos(perp), ey + (TAIL_W1 + 4) * math.sin(perp))
    b2 = (ex - (TAIL_W1 + 4) * math.cos(perp), ey - (TAIL_W1 + 4) * math.sin(perp))
    out.append(
        f'<polygon points="{b1[0]:.1f},{b1[1]:.1f} {b2[0]:.1f},{b2[1]:.1f} '
        f'{tipx:.1f},{tipy:.1f}" fill="url(#sting)" stroke="{AMBER}" '
        f'stroke-width="2.5" stroke-linejoin="round"/>'
    )
    out.append(
        f'<circle cx="{tipx:.1f}" cy="{tipy:.1f}" r="10" fill="{AMBER_HOT}" '
        f'filter="url(#glow)"/>'
    )
    return out


def legs() -> list[str]:
    out = []
    for y, angle, length in [(298.0, 16.0, 78.0), (326.0, 34.0, 84.0),
                             (352.0, 52.0, 76.0)]:
        for side in (-1, 1):
            a = math.radians(angle)
            hx = CX + side * 26
            kx = hx + side * length * 0.62
            ky = y + length * 0.62 * math.sin(a)
            fx = kx + side * length * 0.42
            fy = ky + length * 0.5
            d = (f"M{hx:.0f},{y:.0f} L{kx:.0f},{ky:.0f} L{fx:.0f},{fy:.0f}")
            out.append(f'<path d="{d}" fill="none" stroke="{PLATE_HI}" '
                       f'stroke-width="15" stroke-linecap="round" '
                       f'stroke-linejoin="round"/>')
            out.append(f'<path d="{d}" fill="none" stroke="{EDGE_DIM}" '
                       f'stroke-width="3" stroke-linecap="round" '
                       f'stroke-linejoin="round" opacity="0.85"/>')
            out.append(f'<circle cx="{kx:.0f}" cy="{ky:.0f}" r="5.5" '
                       f'fill="{EDGE_DIM}"/>')
    return out


def claw(side: int) -> list[str]:
    """A forward-reaching pedipalp ending in an open pincer."""
    sx, sy = CX + side * 46, 252.0            # shoulder, at the body
    ex, ey = CX + side * 112, 208.0           # elbow, out to the side
    hx, hy = CX + side * 122, 150.0           # pincer hinge, forward
    arm = f"M{sx:.0f},{sy:.0f} L{ex:.0f},{ey:.0f} L{hx:.0f},{hy:.0f}"
    out = [
        f'<path d="{arm}" fill="none" stroke="{PLATE_HI}" stroke-width="26" '
        f'stroke-linecap="round" stroke-linejoin="round"/>',
        f'<path d="{arm}" fill="none" stroke="{EDGE_DIM}" stroke-width="4.5" '
        f'stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>',
    ]
    # Two jaws opening forward/up, clearly a pincer rather than a fork.
    jaws = [
        (hx + side * 52, hy - 54, hx + side * 58, hy - 4),     # outer
        (hx - side * 20, hy - 66, hx + side * 4, hy - 16),     # inner
    ]
    for tipx, tipy, ctrlx, ctrly in jaws:
        d = f"M{hx:.0f},{hy:.0f} Q{ctrlx:.0f},{ctrly:.0f} {tipx:.0f},{tipy:.0f}"
        out.append(f'<path d="{d}" fill="none" stroke="{PLATE_HI}" '
                   f'stroke-width="19" stroke-linecap="round"/>')
        out.append(f'<path d="{d}" fill="none" stroke="{EDGE}" '
                   f'stroke-width="3" stroke-linecap="round" opacity="0.65"/>')
    out.append(f'<circle cx="{hx:.0f}" cy="{hy:.0f}" r="14" fill="{PLATE}" '
               f'stroke="{EDGE_DIM}" stroke-width="3.5"/>')
    return out


def body() -> list[str]:
    out = []
    for y, w in [(292, 70), (320, 64), (346, 56), (370, 46)]:
        out.append(f'<rect x="{CX - w / 2:.0f}" y="{y}" width="{w}" height="24" '
                   f'rx="9" fill="{PLATE}" stroke="{EDGE_DIM}" stroke-width="2"/>')
        out.append(f'<line x1="{CX - w / 2 + 11:.0f}" y1="{y + 12}" '
                   f'x2="{CX + w / 2 - 11:.0f}" y2="{y + 12}" stroke="{EDGE}" '
                   f'stroke-width="2" opacity="0.4"/>')
    out.append(f'<polygon points="{hexagon(CX, 258, 58, 90)}" fill="{PLATE_HI}" '
               f'stroke="{EDGE}" stroke-width="3.5"/>')
    out.append(f'<path d="M{CX - 32:.0f},240 h20 v-15 h24 v15 h20" fill="none" '
               f'stroke="{EDGE}" stroke-width="2.5" opacity="0.5" '
               f'stroke-linecap="round" stroke-linejoin="round"/>')
    out.append(f'<path d="M{CX - 26:.0f},282 h22 v13 h14" fill="none" '
               f'stroke="{EDGE}" stroke-width="2.5" opacity="0.35" '
               f'stroke-linecap="round" stroke-linejoin="round"/>')
    out.append(f'<circle cx="{CX:.0f}" cy="260" r="18" fill="#0c141d" '
               f'stroke="{EDGE}" stroke-width="3.5"/>')
    out.append(f'<circle cx="{CX:.0f}" cy="260" r="9" fill="{AMBER}" '
               f'filter="url(#glow)"/>')
    out.append(f'<circle cx="{CX - 3:.0f}" cy="257" r="3.2" fill="{AMBER_HOT}"/>')
    return out


def svg() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}">',
        "<defs>",
        f'<radialGradient id="bg" cx="50%" cy="44%" r="74%">'
        f'<stop offset="0%" stop-color="{BG_INNER}"/>'
        f'<stop offset="100%" stop-color="{BG_OUTER}"/></radialGradient>',
        f'<linearGradient id="sting" x1="0" y1="1" x2="0" y2="0">'
        f'<stop offset="0%" stop-color="{AMBER}"/>'
        f'<stop offset="100%" stop-color="{AMBER_HOT}"/></linearGradient>',
        '<filter id="glow" x="-140%" y="-140%" width="380%" height="380%">'
        '<feGaussianBlur stdDeviation="7" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/>'
        "</feMerge></filter>",
        "</defs>",
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
        '<g opacity="0.12" stroke="#4fd6e0" stroke-width="1">',
    ]
    for i in range(1, 8):
        parts.append(f'<line x1="0" y1="{i * 64}" x2="{W}" y2="{i * 64}"/>')
        parts.append(f'<line x1="{i * 64}" y1="0" x2="{i * 64}" y2="{H}"/>')
    parts.append("</g>")
    parts += legs()
    parts += tail()
    parts += claw(-1)
    parts += claw(1)
    parts += body()
    parts.append("</svg>")
    return "\n".join(parts)


if __name__ == "__main__":
    print(svg())
