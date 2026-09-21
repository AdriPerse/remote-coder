"""Render the robot-scorpion PNG as terminal half-block art.

Each terminal cell shows TWO vertical pixels: foreground color = top
pixel, background color = bottom pixel, glyph UPPER HALF BLOCK. That is
how CLIs like Cortex show rich mascots. The magenta background of the
source PNG is treated as transparent (dropped to a space).

Usage: python tools/img_to_banner.py <image> [--width N] [--write]
  --write replaces BANNER in cli.py with the rendered art.
"""
import argparse
import re
import sys
from pathlib import Path

from PIL import Image

UPPER = "▀"

# The source art's flat magenta background. Real detection is by hue:
# magenta = high red, high blue, LOW green. Tolerance-based RGB distance
# leaves an anti-aliased fringe; green-dominance does not.
def is_bg(pixel):
    r, g, b = pixel
    return r > 120 and b > 120 and g < 110


def load_pixels(path, width):
    img = Image.open(path).convert("RGB")
    # Crop to the non-background bounding box so the mascot fills the art.
    mask = Image.new("L", img.size, 0)
    mask.putdata([0 if is_bg(p) else 255 for p in img.getdata()])
    box = mask.getbbox()
    if box:
        img = img.crop(box)
    w, h = img.size
    height = max(2, round(width * h / w))
    if height % 2:
        height += 1
    img = img.resize((width, height), Image.LANCZOS)
    return img, width, height


def render(img, width, height):
    lines = []
    for y in range(0, height, 2):
        out = []
        for x in range(width):
            top = img.getpixel((x, y))
            bottom = img.getpixel((x, y + 1))
            t_bg, b_bg = is_bg(top), is_bg(bottom)
            if t_bg and b_bg:
                out.append("\x1b[0m ")
            elif not t_bg and not b_bg:
                out.append(
                    "\x1b[38;2;{};{};{}m\x1b[48;2;{};{};{}m{}\x1b[0m".format(
                        *top, *bottom, UPPER
                    )
                )
            elif not t_bg:  # only top: upper half in fg
                out.append("\x1b[38;2;{};{};{}m{}\x1b[0m".format(*top, UPPER))
            else:  # only bottom: lower half
                out.append("\x1b[38;2;{};{};{}m{}\x1b[0m".format(*bottom, "▄"))
        lines.append("".join(out).rstrip())
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--width", type=int, default=48)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    img, w, h = load_pixels(args.image, args.width)
    art = render(img, w, h)

    if args.write:
        path = Path(__file__).resolve().parent.parent / "cli.py"
        text = path.read_text(encoding="utf-8")
        banner = 'BANNER = """\n' + art + '\n\n      R E M O T E   C O D E R\n"""'
        new = re.sub(r'BANNER = """.*?"""', banner, text, count=1, flags=re.S)
        if new == text:
            sys.exit("BANNER block not found in cli.py")
        path.write_text(new, encoding="utf-8", newline="\n")
        print("cli.py updated ({}x{} cells)".format(w, h // 2))
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(art)


if __name__ == "__main__":
    main()
