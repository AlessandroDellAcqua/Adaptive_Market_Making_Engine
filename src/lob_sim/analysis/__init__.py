"""Run summaries and dependency-free SVG diagnostics."""

from lob_sim.analysis.metrics import compare_runs, quantile, summarize_runs
from lob_sim.analysis.plotting import write_comparison_svg

__all__ = ["compare_runs", "quantile", "summarize_runs", "write_comparison_svg"]

