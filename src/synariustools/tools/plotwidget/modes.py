"""Declarative configuration for :class:`DataViewerWidget` behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PlotViewerMode:
    """Toolbar, legend columns, and layout defaults for the plot widget."""

    name: Literal["static", "dynamic"]
    show_value_column: bool
    show_clear_action: bool
    legend_visible_by_default: bool
    min_plot_width: int
    min_legend_width: int
    max_legend_width: int
    legend_split_saved: int

    @classmethod
    def static(cls, *, legend_visible_by_default: bool = True) -> PlotViewerMode:
        return cls(
            name="static",
            show_value_column=False,
            show_clear_action=True,
            legend_visible_by_default=legend_visible_by_default,
            min_plot_width=420,
            min_legend_width=260,
            max_legend_width=360,
            legend_split_saved=380,
        )

    @classmethod
    def dynamic(cls) -> PlotViewerMode:
        return cls(
            name="dynamic",
            show_value_column=True,
            show_clear_action=False,
            legend_visible_by_default=False,
            min_plot_width=420,
            min_legend_width=340,
            max_legend_width=460,
            legend_split_saved=460,
        )

    @classmethod
    def from_keyword(cls, mode: Literal["static", "dynamic"]) -> PlotViewerMode:
        if mode == "static":
            return cls.static()
        return cls.dynamic()


def resolve_mode(
    mode: PlotViewerMode | Literal["static", "dynamic"],
    *,
    legend_visible_at_start: bool | None,
) -> PlotViewerMode:
    """Apply optional startup legend override to a mode (static or dynamic config object)."""
    if isinstance(mode, PlotViewerMode):
        if legend_visible_at_start is None:
            return mode
        return PlotViewerMode(
            name=mode.name,
            show_value_column=mode.show_value_column,
            show_clear_action=mode.show_clear_action,
            legend_visible_by_default=legend_visible_at_start,
            min_plot_width=mode.min_plot_width,
            min_legend_width=mode.min_legend_width,
            max_legend_width=mode.max_legend_width,
            legend_split_saved=mode.legend_split_saved,
        )
    base = PlotViewerMode.from_keyword(mode)
    if legend_visible_at_start is None:
        return base
    return PlotViewerMode(
        name=base.name,
        show_value_column=base.show_value_column,
        show_clear_action=base.show_clear_action,
        legend_visible_by_default=legend_visible_at_start,
        min_plot_width=base.min_plot_width,
        min_legend_width=base.min_legend_width,
        max_legend_width=base.max_legend_width,
        legend_split_saved=base.legend_split_saved,
    )
