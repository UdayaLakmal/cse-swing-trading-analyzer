"""Server-rendered SVG charts. No JS library, works offline, follows the theme
through CSS variables."""

from __future__ import annotations

import math
from html import escape

from markupsafe import Markup


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step) * step
    ticks, v = [], start
    while v <= hi + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"


def price_chart(days: list[str], close: list[float | None], series: dict[str, tuple[list[float | None], str]],
                levels: dict[str, tuple[float, str]], width: int = 820, height: int = 280) -> Markup:
    """Close line plus overlay series (name -> (values, css colour)) and
    horizontal levels (label -> (price, css colour))."""
    pts = [v for v in close if v is not None]
    if len(pts) < 2:
        return Markup('<p class="muted">Not enough history to chart.</p>')
    allv = pts + [v for vals, _ in series.values() for v in vals if v is not None] + [p for p, _ in levels.values()]
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 56, 12, 12, 26
    w, h = width - left - right, height - top - bottom
    n = len(days)

    def x(i):
        return left + w * i / (n - 1)

    def y(v):
        return top + h * (1 - (v - lo) / (hi - lo))

    def path(vals):
        d, pen = [], False
        for i, v in enumerate(vals):
            if v is None:
                pen = False
                continue
            d.append(f"{'L' if pen else 'M'}{x(i):.1f},{y(v):.1f}")
            pen = True
        return " ".join(d)

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Price chart">']
    for t in _nice_ticks(lo, hi):
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>')
        out.append(f'<text x="{left - 8}" y="{y(t) + 4:.1f}" text-anchor="end" class="axis">{_fmt(t)}</text>')
    last_month = None
    for i, d in enumerate(days):
        month = d[:7]
        if month != last_month and i > 0:
            label = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(d[5:7]) - 1]
            out.append(f'<text x="{x(i):.1f}" y="{height - 6}" text-anchor="middle" class="axis">{label}</text>')
        last_month = month
    # Area under the close for a softer look.
    area = path(close)
    if area:
        first = next(i for i, v in enumerate(close) if v is not None)
        out.append(f'<path d="{area} L{x(n - 1):.1f},{top + h} L{x(first):.1f},{top + h} Z" class="area"/>')
    for name, (vals, colour) in series.items():
        out.append(f'<path d="{path(vals)}" fill="none" stroke="{colour}" stroke-width="1.4" stroke-dasharray="4 3"><title>{escape(name)}</title></path>')
    out.append(f'<path d="{area}" fill="none" class="line"/>')
    for label, (price, colour) in levels.items():
        if lo <= price <= hi:
            out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(price):.1f}" y2="{y(price):.1f}" stroke="{colour}" stroke-width="1.2"/>')
            out.append(f'<text x="{width - right - 4}" y="{y(price) - 5:.1f}" text-anchor="end" class="lvl" fill="{colour}">{escape(label)} {_fmt(price)}</text>')
    out.append(f'<circle cx="{x(n - 1):.1f}" cy="{y(pts[-1]):.1f}" r="3.5" class="dot"/>')
    out.append("</svg>")
    return Markup("".join(out))


def sparkline(values: list[float], width: int = 96, height: int = 28) -> Markup:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return Markup("")
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = width / (len(vals) - 1)
    d = " ".join(f"{'M' if i == 0 else 'L'}{i * step:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}" for i, v in enumerate(vals))
    cls = "up" if vals[-1] >= vals[0] else "down"
    return Markup(f'<svg viewBox="0 0 {width} {height}" class="spark {cls}" aria-hidden="true"><path d="{d}"/></svg>')
