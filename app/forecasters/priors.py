"""Calibration priors — the forecast's only grounding.

Loads the committed snapshot of the real-world calibration priors
(`ottoq_calibration_*` tables in the engine DB, read-only) and validates it
against its content fingerprint.

THE CONTAMINATION GUARD, MADE STRUCTURAL
----------------------------------------
This module — and everything under `app/forecasters/` — reads ONLY the
calibration priors. The priors are fitted from public, external datasets
(ACN-Data, NYC TLC, EIA hourly grid, NREL Fleet DNA), never from OTTO-Q's
simulation or decision output. There is no code path anywhere in this package
to `ottoq_decisions`, `ottoq_vehicle_dispatches`, `site_energy_snapshots`,
`ottoq_service_detail_records`, or any other sim/decision table or object.

Consequence: the forecast is a pure function of (priors, site, horizon, seed).
It cannot learn from the sandbox because it cannot see the sandbox. A buggy
orchestration layer — however many churn/oscillation defects it has shipped —
cannot poison the forecast, because the forecast never observes its decisions.

This is the founder's caveat ("the twin data can be skewed because it replays
OTTO-Q's decisions"), answered at the architecture level rather than by a
promise. The forecast models *demand* (arrivals, energy requirement, weather,
grid shape) — the world physics OTTO-Q does not control — and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).parent / "priors_snapshot.json"


@dataclass(frozen=True)
class DatasetMeta:
    dataset_code: str
    source_name: str
    source_org: str
    source_url: str | None
    license: str | None
    domain: str
    what_it_calibrates: str


@dataclass(frozen=True)
class Profile:
    """A cyclical shape (hourly_24 / daily_7) normalized to mean 1.0."""
    dataset_code: str
    profile_name: str
    profile_kind: str
    units: str
    data: dict[str, float]


@dataclass(frozen=True)
class Distribution:
    """A 101-point empirical/parametric quantile grid with hard bounds."""
    dataset_code: str
    variable_name: str
    segment: str
    units: str
    sample_count: int
    mean_value: float | None
    stddev_value: float | None
    hard_min: float | None
    hard_max: float | None
    best_fit_family: str
    quantile_grid: list[float]
    fitted_at: str | None


@dataclass(frozen=True)
class Priors:
    datasets: dict[str, DatasetMeta]
    profiles: dict[str, dict[str, Profile]]
    distributions: dict[str, dict[str, Distribution]]
    fingerprint: str
    generated_at: str


#: Provenance-only fields inside a distribution: metadata about WHEN a fit ran,
#: not about what it says. Excluded from the content hash (finding L-34).
METADATA_FIELDS = ("fitted_at",)


def fingerprint(content: dict) -> str:
    """md5 over the canonical content — same discipline as the engine's
    `ottoq_calibration_fingerprint()` (0201): a refit that lands the same
    numbers is not a change to the world, so generated_at / fitted_at are
    excluded from the hash where they are metadata rather than content.

    THAT SENTENCE WAS FALSE UNTIL 09-08 (finding L-34). Nothing excluded
    anything: `load_priors` built the content as the three raw blocks and
    hashed them whole, and every `distributions` entry carries a `fitted_at`
    wall-clock timestamp copied from the engine DB. So a re-snapshot of
    NUMERICALLY IDENTICAL priors changed the fingerprint, and the fingerprint
    is not inert — it is stamped into every /forecast response as
    `priors_fingerprint`, which is how a forecast is identified. Every forecast
    got a new identity because a refit job ran, not because a number moved.
    `_canonical_priors` now performs the exclusion the docstring describes; the
    engine's own function has always done it ("Content only; timestamps are
    deliberately left out").
    """
    blob = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.md5(blob).hexdigest()


def _canonical_priors(raw: dict) -> dict:
    """The three content blocks, with per-distribution metadata stripped."""
    distributions = {
        code: {name: {k: v for k, v in d.items() if k not in METADATA_FIELDS}
               for name, d in ds.items()}
        for code, ds in raw["distributions"].items()
    }
    return {"datasets": raw["datasets"], "profiles": raw["profiles"],
            "distributions": distributions}


def _as_float(value, *, field: str, where: str):
    """A numeric prior as a float, or None. Raises rather than carrying a str.

    Every one of mean_value / stddev_value / hard_min / hard_max is a JSON
    STRING in the committed snapshot, and all four were assigned straight
    through `d.get(...)` into a dataclass annotated `float | None` (finding
    L-54). `statistical.py` only survived because it wraps two of them in
    `float()` at the point of use; the other two were never read at all.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"priors {where}: {field} is not numeric "
                         f"({value!r})") from exc


def _validate_distribution(where: str, dist) -> None:
    """The declared bounds become an ENFORCED INVARIANT, not documentation.

    `hard_min` / `hard_max` were loaded and then read by no code path at all,
    so the bounds every distribution declares were decorative and the forecast
    never checked a quantile against them (finding L-54). A non-monotone grid
    is worse than a wrong one: the quantile lookup walks it in order, so p90
    could come back below p50 and every band built on it would be inverted
    without anything raising.
    """
    grid = dist.quantile_grid
    if not grid:
        raise ValueError(f"priors {where}: empty quantile_grid")
    for a, b in zip(grid, grid[1:]):
        if b < a:
            raise ValueError(
                f"priors {where}: quantile_grid is not monotone "
                f"non-decreasing ({a} then {b}) — a lookup walks it in order, "
                f"so a later quantile would return a smaller value")
    lo, hi = dist.hard_min, dist.hard_max
    if lo is not None and grid[0] < lo:
        raise ValueError(f"priors {where}: quantile_grid starts at {grid[0]}, "
                         f"below its declared hard_min {lo}")
    if hi is not None and grid[-1] > hi:
        raise ValueError(f"priors {where}: quantile_grid ends at {grid[-1]}, "
                         f"above its declared hard_max {hi}")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"priors {where}: hard_min {lo} exceeds hard_max {hi}")


def load_priors(path: str | Path = SNAPSHOT_PATH) -> Priors:
    raw = json.loads(Path(path).read_text())
    manifest = raw["manifest"]

    datasets = {
        code: DatasetMeta(
            dataset_code=code,
            source_name=m["source_name"],
            source_org=m["source_org"],
            source_url=m.get("source_url"),
            license=m.get("license"),
            domain=m["domain"],
            what_it_calibrates=m["what_it_calibrates"],
        )
        for code, m in raw["datasets"].items()
    }

    profiles: dict[str, dict[str, Profile]] = {}
    for code, ps in raw["profiles"].items():
        profiles[code] = {
            name: Profile(
                dataset_code=code, profile_name=name,
                profile_kind=p["profile_kind"], units=p["units"],
                data={str(k): float(v) for k, v in p["data"].items()},
            )
            for name, p in ps.items()
        }

    distributions: dict[str, dict[str, Distribution]] = {}
    for code, ds in raw["distributions"].items():
        distributions[code] = {
            name: Distribution(
                dataset_code=code, variable_name=name,
                segment=d["segment"], units=d["units"],
                sample_count=int(d["sample_count"]),
                mean_value=_as_float(d.get("mean_value"),
                                     field="mean_value", where=f"{code}.{name}"),
                stddev_value=_as_float(d.get("stddev_value"),
                                       field="stddev_value", where=f"{code}.{name}"),
                hard_min=_as_float(d.get("hard_min"),
                                   field="hard_min", where=f"{code}.{name}"),
                hard_max=_as_float(d.get("hard_max"),
                                   field="hard_max", where=f"{code}.{name}"),
                best_fit_family=d["best_fit_family"],
                quantile_grid=[float(x) for x in d["quantile_grid"]],
                fitted_at=d.get("fitted_at"),
            )
            for name, d in ds.items()
        }
    for code, ds in distributions.items():
        for name, dist in ds.items():
            _validate_distribution(f"{code}.{name}", dist)

    priors = Priors(
        datasets=datasets, profiles=profiles, distributions=distributions,
        fingerprint=manifest["fingerprint_md5"],
        generated_at=manifest["generated_at_utc"],
    )

    # Verify the snapshot's content hash before trusting it. A snapshot that
    # does not match its own fingerprint is a tampered or truncated artifact and
    # must be refused, not used.
    actual = fingerprint(_canonical_priors(raw))
    if actual != priors.fingerprint:
        raise ValueError(
            f"priors snapshot fingerprint mismatch: manifest says "
            f"{priors.fingerprint}, content hashes to {actual} — refusing to "
            f"forecast on an unverified prior set")
    return priors
