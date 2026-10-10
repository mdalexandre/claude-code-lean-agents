#!/usr/bin/env python3
"""Draw the README diagrams from assets/visuals/spec.json, in a light and a dark version.

Every number comes from the spec file, so the charts and the README table cannot drift
apart. Hand-built SVG in Google's Material colours, standard library only.

Usage:
    python3 scripts/make_visuals.py          # writes assets/visuals/<name>-{light,dark}.svg
    python3 scripts/make_visuals.py --check  # exit 1 if the committed SVGs are out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VIS = ROOT / "assets" / "visuals"
FONT = "Google Sans, Roboto, Segoe UI, Helvetica, Arial, sans-serif"
W = 880
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#FFFFFF",
        "card": "#F8F9FA",
        "border": "#DADCE0",
        "text": "#202124",
        "sub": "#5F6368",
        "faint": "#BDC1C6",
        "track": "#F1F3F4",
        "on": "#FFFFFF",
        "red": "#D93025",
        "green": "#137333",
        "blue": "#1967D2",
        "yellow": "#F9AB00",
        "grey": "#80868B",
    },
    "dark": {
        "bg": "#0D1117",
        "card": "#161B22",
        "border": "#30363D",
        "text": "#E8EAED",
        "sub": "#9AA0A6",
        "faint": "#5F6368",
        "track": "#21262D",
        "on": "#0D1117",
        "red": "#F28B82",
        "green": "#81C995",
        "blue": "#8AB4F8",
        "yellow": "#FDD663",
        "grey": "#9AA0A6",
    },
}


def text(
    x: float, y: float, s: str, size: float, fill: str, weight: int = 400, anchor: str = "start"
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>'
    )


def rect(
    x: float, y: float, w: float, h: float, fill: str, rx: float = 0, stroke: str = "none"
) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{h:.1f}" '
        f'rx="{rx}" fill="{fill}" stroke="{stroke}"/>'
    )


def on_colour(t: dict[str, str], colour: str) -> str:
    """Text colour that stays readable on a filled shape of the given theme colour."""
    return "#202124" if colour == "yellow" else t["on"]


def frame(t: dict[str, str], h: float, title: str, note: str | list[str], body: str) -> str:
    """Wrap a chart body in the card, title and note; a list note becomes several lines."""
    lines = [note] if isinstance(note, str) else note
    first_note_y = h - 22
    h += 19 * (len(lines) - 1)
    notes = "".join(
        text(32, first_note_y + 19 * i, line, 13, t["sub"]) for i, line in enumerate(lines)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h:.0f}" '
        f'viewBox="0 0 {W} {h:.0f}" font-family="{FONT}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>"
        + rect(0.5, 0.5, W - 1, h - 1, t["bg"], 12, t["border"])
        + text(32, 46, title, 21, t["text"], 600)
        + body
        + notes
        + "</svg>\n"
    )


def check_icon(cx: float, cy: float, fill: str, mark: str) -> str:
    return (
        f'<circle cx="{cx}" cy="{cy}" r="10" fill="{fill}"/>'
        f'<path d="M{cx - 4.5} {cy} l3 3.2 l6 -6.4" fill="none" stroke="{mark}" '
        f'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
    )


def cross_icon(cx: float, cy: float, stroke: str) -> str:
    return (
        f'<circle cx="{cx}" cy="{cy}" r="9.5" fill="none" stroke="{stroke}" stroke-width="1.5"/>'
        f'<path d="M{cx - 3.5} {cy - 3.5} l7 7 M{cx + 3.5} {cy - 3.5} l-7 7" '
        f'stroke="{stroke}" stroke-width="1.8" stroke-linecap="round"/>'
    )


def start_chart(spec: dict[str, Any], t: dict[str, str]) -> str:
    rows = spec["rows"]
    base = next(r["value"] for r in rows if r.get("baseline"))
    top, row_h, x0, bar_w = 84, 46, 230, 470
    scale = bar_w / max(r["value"] for r in rows)
    body = ""
    for i, r in enumerate(rows):
        y = top + i * row_h
        weight = 600 if r.get("bold") else 400
        body += text(32, y + 17, r["label"], 15, t["text"], weight)
        body += rect(x0, y, bar_w, 24, t["track"], 6)
        body += rect(x0, y, r["value"] * scale, 24, t[r["color"]], 6)
        label_x = x0 + r["value"] * scale + 10
        body += text(label_x, y + 17, f"{r['value']:.1f}k", 14, t["text"], 600)
        if r.get("compare"):
            body += text(
                label_x + 52, y + 17, f"about {base / r['value']:.0f}x less", 13, t[r["color"]], 600
            )
    h = top + len(rows) * row_h + 44
    return frame(t, h, spec["title"], spec["note"], body)


def loads_chart(spec: dict[str, Any], t: dict[str, str]) -> str:
    cards = spec["cards"]
    top, card_w, gap, step = 76, 392, 32, 34
    n = max(len(c["items"]) for c in cards)
    card_h = 70 + n * step + 96
    body = ""
    for i, c in enumerate(cards):
        x = 32 + i * (card_w + gap)
        colour = t[c["color"]]
        body += rect(x, top, card_w, card_h, t["card"], 12, t["border"])
        body += f'<circle cx="{x + 30}" cy="{top + 32}" r="7" fill="{colour}"/>'
        body += text(x + 46, top + 38, c["name"], 17, t["text"], 600)
        for j, (label, kind) in enumerate(c["items"]):
            cy = top + 78 + j * step
            if kind == "skip":
                body += cross_icon(x + 30, cy, t["faint"])
                body += text(x + 52, cy + 5, label, 15, t["sub"])
                body += text(x + card_w - 20, cy + 5, "not loaded", 13, t["sub"], 400, "end")
            else:
                fill = (
                    t["red"] if kind == "heavy" else colour if c["color"] == "green" else t["blue"]
                )
                body += check_icon(x + 30, cy, fill, t["on"])
                body += text(x + 52, cy + 5, label, 15, t["text"])
        total_y = top + 70 + n * step + 40
        body += text(x + 24, total_y, c["total"], 34, colour, 700)
        body += text(x + 24, total_y + 26, c["total_note"], 13, t["sub"])
    h = top + card_h + 56
    return frame(t, h, spec["title"], spec["note"], body)


def turns_chart(spec: dict[str, Any], t: dict[str, str]) -> str:
    n = spec["turns"]
    rows = spec["rows"]
    top, row_h, x0, bar_w = 104, 66, 200, 500
    scale = bar_w / max(r["per_turn"] * n for r in rows)
    body = text(32, 72, spec["subtitle"], 14, t["sub"])
    for i, r in enumerate(rows):
        y = top + i * row_h
        weight = 600 if r.get("bold") else 400
        body += text(32, y + 21, r["label"], 15, t["text"], weight)
        seg = r["per_turn"] * scale
        body += rect(x0, y, bar_w, 30, t["track"], 6)
        body += rect(x0, y, seg * n, 30, t[r["color"]], 6)
        if seg >= 12:
            for k in range(1, n):
                sx = x0 + k * seg
                body += (
                    f'<line x1="{sx:.1f}" x2="{sx:.1f}" y1="{y}" y2="{y + 30}" '
                    f'stroke="{t["bg"]}" stroke-width="2"/>'
                )
            body += text(x0 + seg / 2, y + 48, "1 turn", 12, t["sub"], 400, "middle")
        total = r["per_turn"] * n
        value = f"{total / 1000:.2f}M" if total >= 1000 else f"{total:.0f}k"
        body += text(x0 + seg * n + 10, y + 21, f"{value} tokens", 14, t["text"], 600)
    h = top + len(rows) * row_h + 34
    return frame(t, h, spec["title"], spec["note"], body)


def which_chart(spec: dict[str, Any], t: dict[str, str]) -> str:
    qx, qw, qh, rx, rw = 32, 400, 56, 520, 328
    cx = qx + qw / 2
    arrow = t["sub"]
    body = (
        f'<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto">'
        f'<path d="M0 0 L10 5 L0 10 z" fill="{arrow}"/></marker></defs>'
    )

    def line(x1: float, y1: float, x2: float, y2: float) -> str:
        return (
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{arrow}" '
            f'stroke-width="1.6" marker-end="url(#a)"/>'
        )

    def pill(x: float, y: float, w: float, h: float, label: str, colour: str) -> str:
        return rect(x, y, w, h, t[colour], h / 2) + text(
            x + w / 2, y + h / 2 + 5.5, label, 16, on_colour(t, colour), 600, "middle"
        )

    body += rect(qx + 60, 76, qw - 120, 40, t["card"], 20, t["border"])
    body += text(cx, 101, spec["start"], 15, t["text"], 400, "middle")
    y = 146
    body += line(cx, 116, cx, y - 2)
    for i, q in enumerate(spec["questions"]):
        body += rect(qx, y, qw, qh, t["card"], 10, t["border"])
        body += text(cx, y + qh / 2 + 5.5, q["q"], 16, t["text"], 500, "middle")
        mid = y + qh / 2
        body += line(qx + qw, mid, rx - 4, mid) + text(
            (qx + qw + rx) / 2, mid - 8, "Yes", 13, t["sub"], 600, "middle"
        )
        body += pill(rx, mid - 24, rw, 48, q["yes"], q["color"])
        next_y = y + qh + 44
        body += line(cx, y + qh, cx, next_y - 2) + text(
            cx + 10, y + qh + 27, "No", 13, t["sub"], 600
        )
        y = next_y
    body += pill(qx + 40, y, qw - 80, 48, spec["otherwise"], spec["otherwise_color"])
    h = y + 48 + 56
    return frame(t, h, spec["title"], spec["note"], body)


CHARTS = {"start": start_chart, "loads": loads_chart, "turns": turns_chart, "which": which_chart}


def render_all(spec: dict[str, Any]) -> dict[Path, str]:
    out = {}
    for name, draw in CHARTS.items():
        for theme, palette in THEMES.items():
            out[VIS / f"{name}-{theme}.svg"] = draw(spec[name], palette)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the SVG files are out of date")
    args = ap.parse_args()
    spec = json.loads((VIS / "spec.json").read_text(encoding="utf-8"))
    files = render_all(spec)
    stale = [p for p, s in files.items() if not p.exists() or p.read_text(encoding="utf-8") != s]
    if args.check:
        for p in stale:
            print(f"out of date: {p.relative_to(ROOT)}", file=sys.stderr)
        return 1 if stale else 0
    for p, s in files.items():
        p.write_text(s, encoding="utf-8")
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
