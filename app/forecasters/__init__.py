"""Forecasters — the statistical, demand-side forecast (FR-2).

This package is deliberately self-contained: it reads only the committed
calibration-priors snapshot (real-world datasets) and produces a probabilistic
forecast of arrivals / load / SoC return. See `priors.py` for the contamination
guard that makes it structurally incapable of learning from OTTO-Q decision or
simulation output.
"""

from app.forecasters.priors import load_priors  # noqa: F401
from app.forecasters.statistical import forecast  # noqa: F401

__all__ = ["load_priors", "forecast"]
