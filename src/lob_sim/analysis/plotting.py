"""Small dependency-free SVG plots for reproducible experiment artifacts."""

from __future__ import annotations

import html
from collections.abc import Iterable
from pathlib import Path

from lob_sim.simulation import RunResult

_COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e")


def write_comparison_svg(runs: Iterable[RunResult], path: str | Path) -> None:
    """Write wealth and inventory trajectories for one run per strategy."""

    representative: dict[str, RunResult] = {}
    for run in runs:
        representative.setdefault(run.strategy_name, run)
    if not representative:
        raise ValueError("at least one run is required")

    selected = list(representative.values())
    width, height = 1100, 720
    left, right = 90, 1035
    top, chart_height = 75, 250
    gap = 105
    second_top = top + chart_height + gap
    all_wealth = [value for run in selected for value in run.wealth]
    all_inventory = [value for run in selected for value in run.inventory]
    wealth_min, wealth_max = _bounds(all_wealth)
    inventory_min, inventory_max = _bounds(all_inventory)
    max_time = max(run.timestamps[-1] for run in selected)

    elements: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#222} .axis{stroke:#555;stroke-width:1} '
        '.grid{stroke:#ddd;stroke-width:1} .title{font-size:22px;font-weight:bold} '
        '.label{font-size:14px} .legend{font-size:13px}</style>',
        '<text x="90" y="38" class="title">Adaptive Market Making: representative path</text>',
    ]
    _add_chart(
        elements,
        selected,
        value_getter=lambda run: run.wealth,
        y_min=wealth_min,
        y_max=wealth_max,
        title="Mark-to-market wealth",
        y_label="wealth",
        top=top,
        chart_height=chart_height,
        left=left,
        right=right,
        max_time=max_time,
        value_format=".2f",
    )
    _add_chart(
        elements,
        selected,
        value_getter=lambda run: run.inventory,
        y_min=inventory_min,
        y_max=inventory_max,
        title="Inventory trajectory",
        y_label="contracts",
        top=second_top,
        chart_height=chart_height,
        left=left,
        right=right,
        max_time=max_time,
        value_format=".0f",
    )
    elements.append("</svg>")
    Path(path).write_text("\n".join(elements), encoding="utf-8")


def _bounds(values: list[float] | list[int]) -> tuple[float, float]:
    lower = float(min(values))
    upper = float(max(values))
    if upper == lower:
        pad = max(1.0, abs(lower) * 0.01)
        return lower - pad, upper + pad
    pad = (upper - lower) * 0.08
    return lower - pad, upper + pad


def _add_chart(
    elements: list[str],
    runs: list[RunResult],
    *,
    value_getter,
    y_min: float,
    y_max: float,
    title: str,
    y_label: str,
    top: int,
    chart_height: int,
    left: int,
    right: int,
    max_time: float,
    value_format: str,
) -> None:
    bottom = top + chart_height
    elements.append(f'<text x="{left}" y="{top - 20}" class="label">{html.escape(title)}</text>')
    for grid_index in range(5):
        y = top + chart_height * grid_index / 4
        value = y_max - (y_max - y_min) * grid_index / 4
        elements.append(f'<line x1="{left}" x2="{right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        elements.append(
            f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" class="legend">'
            f'{value:{value_format}}</text>'
        )
    elements.append(f'<line x1="{left}" x2="{right}" y1="{bottom}" y2="{bottom}" class="axis"/>')
    elements.append(f'<line x1="{left}" x2="{left}" y1="{top}" y2="{bottom}" class="axis"/>')
    elements.append(
        f'<text x="{left - 60}" y="{top + chart_height / 2}" transform="rotate(-90 {left - 60} '
        f'{top + chart_height / 2})" class="label">{html.escape(y_label)}</text>'
    )
    for index, run in enumerate(runs):
        color = _COLORS[index % len(_COLORS)]
        points: list[str] = []
        values = value_getter(run)
        for timestamp, value in zip(run.timestamps, values, strict=True):
            x = left + (right - left) * timestamp / max_time if max_time else left
            y = bottom - chart_height * (float(value) - y_min) / (y_max - y_min)
            points.append(f"{x:.1f},{y:.1f}")
        elements.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
            'stroke-width="2"/>'
        )
        legend_x = left + index * 180
        legend_y = bottom + 36
        elements.append(
            f'<line x1="{legend_x}" x2="{legend_x + 22}" y1="{legend_y}" '
            f'y2="{legend_y}" stroke="{color}" stroke-width="3"/>'
        )
        elements.append(
            f'<text x="{legend_x + 28}" y="{legend_y + 4}" class="legend">'
            f'{html.escape(run.strategy_name)}</text>'
        )
