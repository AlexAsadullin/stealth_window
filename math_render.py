"""Answers as rich text, with LaTeX math rendered as images instead of raw ``$\\frac{a}{b}$``.

Formulas ($...$, $$...$$, \\(...\\), \\[...\\]) are cut out of the markdown,
the rest goes through Qt's markdown converter, and each formula comes back as
a transparent PNG drawn by matplotlib's mathtext (a LaTeX subset: fractions,
roots, sums, integrals, matrices are not supported). Anything it can't parse
stays as plain text. Code spans and blocks are left untouched.
"""
import hashlib
import html
import re

from PySide6.QtGui import QTextDocument
from platform_support import cache_dir

CACHE = cache_dir() / "math"
COLOR = "#e8e8ea"
FONT_SIZE = 15  # at 72 dpi a point is one Qt pixel; matches the 14 px body text
SCALE = 2  # render at 2x for Retina, display at 1x

_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
_MATH = re.compile(
    r"\$\$(?P<d1>.+?)\$\$"
    r"|\\\[(?P<d2>.+?)\\\]"
    r"|\\\((?P<i1>.+?)\\\)"
    r"|(?<![\\$\w])\$(?![\s$])(?P<i2>[^$\n]+?)(?<!\s)\$(?![\w$])",  # not "$5 and $10"
    re.S)
# Common LaTeX that mathtext lacks, mapped to what it has.
_SUBST = [(r"\dfrac", r"\frac"), (r"\tfrac", r"\frac"), (r"\text{", r"\mathrm{"),
          (r"\operatorname{", r"\mathrm{"), (r"\textbf{", r"\mathbf{"), (r"\le ", r"\leq "),
          (r"\ge ", r"\geq "), (r"\lvert", "|"), (r"\rvert", "|"), (r"\displaystyle", "")]

_cache: dict[tuple[str, bool], tuple[str, int, int] | None] = {}


def markdown_to_html(text: str) -> str:
    formulas: list[tuple[str, str, bool, str]] = []  # (placeholder, original, display, tex)

    def cut(m: re.Match) -> str:
        display = m.group("d1") is not None or m.group("d2") is not None
        tex = next(g for g in m.group("d1", "d2", "i1", "i2") if g is not None)
        key = f"MATHXQ{len(formulas)}QX"  # letters only: survives markdown untouched
        formulas.append((key, m.group(0), display, tex.strip()))
        line_start = m.string.rfind("\n", 0, m.start()) + 1
        line_end = m.string.find("\n", m.end())
        alone = not (m.string[line_start:m.start()].strip() or m.string[m.end():line_end if line_end >= 0 else None].strip())
        return f"\n\n{key}\n\n" if display and alone else key  # mid-sentence: stay in the line (or list item)

    parts, last = [], 0
    for code in _CODE.finditer(text):
        parts.append(_MATH.sub(cut, text[last:code.start()]))
        parts.append(code.group(0))
        last = code.end()
    parts.append(_MATH.sub(cut, text[last:]))

    doc = QTextDocument()
    doc.setMarkdown("".join(parts))
    out = re.sub(r"<body[^>]*>", "<body>", doc.toHtml())  # keep the window's font, not Qt's default
    for key, original, display, tex in formulas:
        image = _render(tex, display)
        if image is None:
            replacement = html.escape(original)
        else:
            path, w, h = image
            replacement = f'<img src="{path}" width="{w}" height="{h}" style="vertical-align: middle">'
        out = out.replace(key, replacement)
    return out


def _render(tex: str, display: bool) -> tuple[str, int, int] | None:
    """(png path, width, height in px) for a formula, or None if mathtext can't draw it."""
    if (tex, display) in _cache:
        return _cache[tex, display]
    result = None
    try:
        result = _draw(tex, display)
    except Exception:
        pass  # unsupported LaTeX: shown as text
    _cache[tex, display] = result
    return result


def _draw(tex: str, display: bool) -> tuple[str, int, int]:
    for old, new in _SUBST:
        tex = tex.replace(old, new)
    tex = " ".join(tex.split())  # mathtext rejects newlines
    path = CACHE / f"{hashlib.sha1(f'{display}|{tex}'.encode()).hexdigest()[:20]}.png"

    import matplotlib  # deferred: importing matplotlib takes about half a second
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import MathTextParser

    matplotlib.rcParams["mathtext.fontset"] = "cm"  # Computer Modern: looks like LaTeX
    prop = FontProperties(size=FONT_SIZE * (1.25 if display else 1.0))
    s = f"${tex}$"
    width, height, depth, _, _ = MathTextParser("path").parse(s, dpi=72, prop=prop)
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        fig = Figure(figsize=(width / 72, height / 72))
        fig.text(0, depth / height, s, fontproperties=prop, color=COLOR)
        fig.savefig(path, dpi=72 * SCALE, transparent=True)
    return str(path), round(width), round(height)


def warm_up():
    """Import matplotlib and its fonts ahead of time (safe off the UI thread: no Qt involved)."""
    _render("x^2", False)
