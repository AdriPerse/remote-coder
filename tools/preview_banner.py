"""Preview the rendered banner as an HTML page (true colors, exact glyphs)."""
import re
import subprocess
import sys
from pathlib import Path

out = subprocess.run(
    [sys.executable, "tools/img_to_banner.py",
     "artifacts/fucking-scorpion.png", "--width", "48"],
    capture_output=True, text=True, encoding="utf-8",
).stdout

def ansi_to_html(s):
    s = s.replace("&", "&amp;").replace("<", "&lt;")
    s = re.sub(r"\x1b\[38;2;(\d+);(\d+);(\d+)m\x1b\[48;2;(\d+);(\d+);(\d+)m",
               r'<span style="color:rgb(\1,\2,\3);background:rgb(\4,\5,\6)">', s)
    s = re.sub(r"\x1b\[38;2;(\d+);(\d+);(\d+)m",
               r'<span style="color:rgb(\1,\2,\3)">', s)
    return s.replace("\x1b[0m", "</span>")

page = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
        "<style>body{background:#16161a;padding:2rem}"
        "pre{line-height:1.0;font-family:Consolas,monospace;font-size:16px;letter-spacing:0}</style>"
        "</head><body><pre>" + ansi_to_html(out) + "</pre></body></html>")
Path("artifacts/banner-preview.html").write_text(page, encoding="utf-8")
print("ok")
