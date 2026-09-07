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


def fingerprint(content: dict) -> str:
    """md5 over the canonical content — same discipline as the engine's
    `ottoq_calibration_fingerprint()` (0201): a refit that lands the same
    numbers is not a change to the world, so generated_at / fitted_at are
    excluded from the hash where they are metadata rather than content."""
    blob = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.md5(blob).hexdigest()


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
                mean_value=d.get("mean_value"),
                stddev_value=d.get("stddev_value"),
                hard_min=d.get("hard_min"), hard_max=d.get("hard_max"),
                best_fit_family=d["best_fit_family"],
                quantile_grid=[float(x) for x in d["quantile_grid"]],
                fitted_at=d.get("fitted_at"),
            )
            for name, d in ds.items()
        }

    priors = Priors(
        datasets=datasets, profiles=profiles, distributions=distributions,
        fingerprint=manifest["fingerprint_md5"],
        generated_at=manifest["generated_at_utc"],
    )

    # Verify the snapshot's content hash before trusting it. A snapshot that
    # does not match its own fingerprint is a tampered or truncated artifact and
    # must be refused, not used.
    content = {"datasets": raw["datasets"], "profiles": raw["profiles"],
               "distributions": raw["distributions"]}
    actual = fingerprint(content)
    if actual != priors.fingerprint:
        raise ValueError(
            f"priors snapshot fingerprint mismatch: manifest says "
            f"{priors.fingerprint}, content hashes to {actual} — refusing to "
            f"forecast on an unverified prior set")
    return priors
