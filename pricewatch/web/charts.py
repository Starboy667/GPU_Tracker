"""Chart geometry for server-rendered SVG. Pure functions: numbers in, coordinates out.

The templates draw what these return; nothing here knows about colours or markup.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

MAX_GAP_DAYS = 7  # a longer hole in the history breaks the line instead of bridging it
MIN_SPAN_DAYS = 6
NBSP = chr(0xA0)  # no-break space: keeps the amount and its € on one line
THIN_NBSP = chr(0x202F)  # narrow no-break space: French thousands separator
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_money(value: float, *, decimals: bool = True) -> str:
    """1079.95 -> '1 079,95 €' (French grouping, as the shops print it)."""
    text = f"{value:,.2f}" if decimals else f"{value:,.0f}"
    return text.replace(",", THIN_NBSP).replace(".", ",") + NBSP + "€"


def money_parts(value: float) -> tuple[str, str]:
    """1079.95 -> ('1 079', '95'): euros and cents apart, for labels that print them at two sizes."""
    euros, cents = f"{value:,.2f}".split(".")
    return euros.replace(",", THIN_NBSP), cents


def fmt_day(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


@dataclass(frozen=True)
class Point:
    day: str  # ISO date
    label: str  # "25 Nov"
    value: float
    text: str  # "919,99 €"
    x: float
    y: float


@dataclass(frozen=True)
class Tick:
    pos: float
    label: str
    anchor: str = "middle"


@dataclass(frozen=True)
class RefLine:
    key: str  # "usual" | "alert"
    y: float
    label: str
    label_x: float
    label_y: float
    label_anchor: str = "start"


@dataclass(frozen=True)
class LineChart:
    width: int
    height: int
    left: float
    right: float
    top: float
    bottom: float
    path: str
    points: tuple[Point, ...]
    lone_points: tuple[Point, ...]  # isolated days: no neighbour to draw a line to
    y_ticks: tuple[Tick, ...]
    x_ticks: tuple[Tick, ...]
    ref_lines: tuple[RefLine, ...]
    end_label_x: float
    end_label_y: float
    end_label_anchor: str
    summary: str

    @property
    def last(self) -> Point:
        return self.points[-1]


@dataclass(frozen=True)
class Sparkline:
    width: int
    height: int
    path: str
    end_x: float
    end_y: float
    summary: str


def nice_ticks(low: float, high: float, target: int = 4) -> list[float]:
    """Round tick values covering [low, high], about ``target`` intervals."""
    if high <= low:
        pad = abs(low) * 0.05 or 1.0
        low, high = low - pad, high + pad
    raw = (high - low) / max(target, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.floor(low / step) * step
    ticks = []
    value = first
    while value < high + step * 0.999:
        ticks.append(round(value, 6))
        value += step
    return ticks


def _in_window(lows: Mapping[str, float], today: date, days: int) -> list[tuple[date, float]]:
    start = today - timedelta(days=days - 1)
    series = []
    for day_text, value in lows.items():
        day = date.fromisoformat(day_text)
        if start <= day <= today:
            series.append((day, float(value)))
    return sorted(series)


def _segments(points: list[Point], days: list[date]) -> list[list[Point]]:
    segments: list[list[Point]] = []
    for index, point in enumerate(points):
        if index and (days[index] - days[index - 1]).days <= MAX_GAP_DAYS:
            segments[-1].append(point)
        else:
            segments.append([point])
    return segments


def _path(segments: list[list[Point]]) -> str:
    parts = []
    for segment in segments:
        if len(segment) < 2:
            continue
        head, *tail = segment
        parts.append(f"M{head.x:.1f} {head.y:.1f}" + "".join(f" L{p.x:.1f} {p.y:.1f}" for p in tail))
    return " ".join(parts)


def _x_ticks(start: date, today: date, x_of, left: float, right: float, max_ticks: int) -> tuple[Tick, ...]:
    span = (today - start).days
    step = next((s for s in (1, 2, 7, 14, 30) if span / s <= max_ticks - 1), 30)
    ticks = []
    day = today
    while day >= start:
        x = x_of(day)
        if x >= left + 18 or day == start:
            anchor = "end" if day == today else ("start" if x < left + 18 else "middle")
            ticks.append(Tick(pos=x, label=fmt_day(day), anchor=anchor))
        day -= timedelta(days=step)
    return tuple(reversed(ticks))


LABEL_CHAR_WIDTH = 6.2  # average glyph width of the 11px label font, in drawing units
LABEL_CLEARANCE = 9.0  # half the height a label needs to stay readable next to the line


def _place_ref_label(
    text: str, y: float, *, prefer_above: bool, points: list[Point], left: float, right: float, top: float, bottom: float
) -> tuple[float, float, str]:
    """Where to write a reference line's label: on the side of the line the data does not cross.

    Tries the preferred side at the left, then at the right; falls back to the preferred side.
    The right end is left free for the latest-value label.
    """
    width = len(text) * LABEL_CHAR_WIDTH
    above, below = y - 6, y + 14
    vertical = [above, below] if prefer_above else [below, above]
    vertical = [v for v in vertical if top + 8 < v < bottom - 2] or [below]
    spans = [(left + 6, left + 6 + width, "start", left + 6)]
    if right - left > 2 * width + 140:
        end = right - 96
        spans.append((end - width, end, "end", end))
    for x0, x1, anchor, x in spans:
        for label_y in vertical if len(spans) == 1 else vertical[:1]:
            centre = label_y - 4
            if not any(x0 - 4 <= p.x <= x1 + 4 and abs(p.y - centre) < LABEL_CLEARANCE for p in points):
                return x, label_y, anchor
    for label_y in vertical[1:]:
        centre = label_y - 4
        x0, x1, anchor, x = spans[0]
        if not any(x0 - 4 <= p.x <= x1 + 4 and abs(p.y - centre) < LABEL_CLEARANCE for p in points):
            return x, label_y, anchor
    x0, x1, anchor, x = spans[0]
    return x, vertical[0], anchor


def line_chart(
    lows: Mapping[str, float],
    *,
    today: date,
    days: int = 90,
    width: int = 720,
    height: int = 260,
    usual: float | None = None,
    alert: float | None = None,
) -> LineChart | None:
    """Daily lows over the last ``days`` days, with optional usual-price and alert reference lines.

    Returns None when there is no point in the window.
    """
    series = _in_window(lows, today, days)
    if not series:
        return None
    left, right, top, bottom = 58.0, width - 14.0, 18.0, height - 30.0
    narrow = width < 500

    references = [(key, value) for key, value in (("usual", usual), ("alert", alert)) if value is not None]
    values = [value for _, value in series] + [value for _, value in references]
    low, high = min(values), max(values)
    pad = (high - low) * 0.1 or high * 0.03 or 1.0
    ticks = nice_ticks(max(low - pad, 0.0), high + pad, target=3 if narrow else 4)
    y_min, y_max = ticks[0], ticks[-1]

    start = min(series[0][0], today - timedelta(days=MIN_SPAN_DAYS))
    span = max((today - start).days, 1)

    def x_of(day: date) -> float:
        return left + (day - start).days / span * (right - left)

    def y_of(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    points = [
        Point(day.isoformat(), fmt_day(day), value, fmt_money(value), round(x_of(day), 1), round(y_of(value), 1))
        for day, value in series
    ]
    segments = _segments(points, [day for day, _ in series])
    whole = all(float(t).is_integer() for t in ticks)

    last = points[-1]
    ordered = sorted(references, key=lambda ref: ref[1], reverse=True)
    ref_lines = []
    for index, (key, value) in enumerate(ordered):
        y = y_of(value)
        text = f"Usual {fmt_money(value)}" if key == "usual" else f"Alert at {fmt_money(value)} or less"
        # The upper line prefers its label above, the lower one below, so the two never collide.
        label_x, label_y, label_anchor = _place_ref_label(
            text, y, prefer_above=index == 0, points=points, left=left, right=right, top=top, bottom=bottom
        )
        ref_lines.append(RefLine(key, round(y, 1), text, round(label_x, 1), round(label_y, 1), label_anchor))

    anchor = "end" if last.x - left > 90 else "start"
    end_x = last.x - 2 if anchor == "end" else last.x + 9
    # The label sits on the side the line does not arrive from: under the dot after a drop,
    # above it otherwise, and always inside the plot.
    arrives_from_above = len(points) > 1 and points[-2].y < last.y
    below, above = last.y + 20, last.y - 11
    if arrives_from_above:
        end_y = below if below < bottom - 4 else above
    else:
        end_y = above if above > top + 4 else below

    lowest = min(series, key=lambda item: item[1])
    summary = (
        f"Lowest available price per day, {points[0].label} to {last.label}. "
        f"Latest {last.text}; lowest {fmt_money(lowest[1])} on {fmt_day(lowest[0])}."
    )
    return LineChart(
        width=width,
        height=height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        path=_path(segments),
        points=tuple(points),
        lone_points=tuple(segment[0] for segment in segments if len(segment) == 1 and segment[0] is not last),
        y_ticks=tuple(Tick(round(y_of(t), 1), fmt_money(t, decimals=not whole)) for t in ticks),
        x_ticks=_x_ticks(start, today, x_of, left, right, 4 if narrow else 7),
        ref_lines=tuple(ref_lines),
        end_label_x=round(end_x, 1),
        end_label_y=round(end_y, 1),
        end_label_anchor=anchor,
        summary=summary,
    )


def sparkline(
    lows: Mapping[str, float], *, today: date, days: int = 30, width: int = 96, height: int = 28
) -> Sparkline | None:
    """A word-sized trend of the daily lows. None with fewer than two points."""
    series = _in_window(lows, today, days)
    if len(series) < 2:
        return None
    pad = 4.0
    values = [value for _, value in series]
    low, high = min(values), max(values)
    start = series[0][0]
    span = max((series[-1][0] - start).days, 1)

    def x_of(day: date) -> float:
        return pad + (day - start).days / span * (width - 2 * pad)

    def y_of(value: float) -> float:
        if high == low:
            return height / 2
        return height - pad - (value - low) / (high - low) * (height - 2 * pad)

    coords = [(x_of(day), y_of(value)) for day, value in series]
    path = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    summary = (
        f"Last {days} days: from {fmt_money(values[0], decimals=False)} to "
        f"{fmt_money(values[-1], decimals=False)}, lowest {fmt_money(low, decimals=False)}"
    )
    return Sparkline(
        width=width, height=height, path=path,
        end_x=round(coords[-1][0], 1), end_y=round(coords[-1][1], 1), summary=summary,
    )
