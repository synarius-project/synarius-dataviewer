"""Data carrier for calibration curve / map visualization."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import numpy as np
from synarius_core.parameters.repository import ParameterRecord


def _parameter_detail_rows(rec: ParameterRecord) -> tuple[tuple[str, str], ...]:
    """Repository fields not shown in the calibration matrix table."""
    vals = np.asarray(rec.values)
    shape_s = str(vals.shape)
    return (
        ("Interner Name", rec.name),
        ("Anzeigename", rec.display_name),
        ("Parameter-ID", str(rec.parameter_id)),
        ("Datensatz-ID", str(rec.data_set_id)),
        ("Kategorie", str(rec.category)),
        ("Werteinheit", rec.unit),
        ("Kommentar", rec.comment),
        ("Konvertierungsreferenz", rec.conversion_ref),
        ("Quellen-Identifier", rec.source_identifier),
        ("Numerisches Format", rec.numeric_format),
        ("Wert-Semantik", rec.value_semantics),
        ("Werte-Shape", shape_s),
    )


@dataclass(frozen=True, slots=True)
class CalibrationMapData:
    """Snapshot of one numeric calibration parameter (curve or map)."""

    title: str
    category: str
    values: np.ndarray
    axes: dict[int, np.ndarray]
    unit: str = ""
    axis_names: dict[int, str] = field(default_factory=dict)
    axis_units: dict[int, str] = field(default_factory=dict)
    detail_rows: tuple[tuple[str, str], ...] = ()
    #: Gesetzt bei Snapshots aus :class:`ParameterRecord` (ParaWiz → Modell/CCP).
    parameter_id: UUID | None = None

    @classmethod
    def from_parameter_record(cls, rec: ParameterRecord) -> CalibrationMapData:
        vals = np.squeeze(np.asarray(rec.values, dtype=np.float64))
        ax_copy = {int(k): np.asarray(v, dtype=np.float64).copy() for k, v in rec.axes.items()}
        return cls(
            title=rec.name,
            category=str(rec.category).upper(),
            values=vals,
            axes=ax_copy,
            unit=str(rec.unit or ""),
            axis_names={int(k): str(v) for k, v in rec.axis_names.items()},
            axis_units={int(k): str(v) for k, v in rec.axis_units.items()},
            detail_rows=_parameter_detail_rows(rec),
            parameter_id=rec.parameter_id,
        )

    def axis_values(self, axis_idx: int) -> np.ndarray:
        if axis_idx in self.axes:
            return np.asarray(self.axes[axis_idx], dtype=np.float64).reshape(-1)
        if axis_idx >= self.values.ndim:
            return np.array([], dtype=np.float64)
        n = int(self.values.shape[axis_idx])
        return np.arange(n, dtype=np.float64)

    def axis_label(self, axis_idx: int, fallback: str) -> str:
        name = self.axis_names.get(axis_idx, "")
        unit = self.axis_units.get(axis_idx, "")
        base = name.strip() or fallback
        return f"{base} [{unit.strip()}]" if unit.strip() else base

    def value_label(self) -> str:
        unit = (self.unit or "").strip()
        return f"Value [{unit}]" if unit else "Value"


def supports_calibration_plot(rec: ParameterRecord) -> bool:
    """True if the record is numeric and at least one-dimensional (curve / map / vector)."""
    if rec.is_text:
        return False
    return int(rec.values.ndim) >= 1


def supports_calibration_scalar_edit(rec: ParameterRecord) -> bool:
    """True if the record is a numeric 0-d scalar (editable in CalibrationMapShell, no plot)."""
    if rec.is_text:
        return False
    return int(np.asarray(rec.values, dtype=np.float64).ndim) == 0
