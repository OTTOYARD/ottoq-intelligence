"""The statistical forecast — probabilistic arrivals / load / SoC return.

Pure functions of (priors, site parameters, horizon, seed). No model training,
no simulation output, no network. Every forecast number carries its provenance
back to the real-world dataset it was fitted from, so an auditor can walk any
prediction to its source.

The three outputs answer the three questions the agentic layer needs to decide
"is tonight tight? pull this wash forward?":

  * arrivals     — how many vehicles return per hour (Poisson, time-varying)
  * load         — expected site kW per hour (base + EV charging, compound-Poisson)
  * soc_return   — the distribution of state-of-charge at return, and the energy
                   need it implies (per vehicle and fleet)

Everything here is DEMAND-side. It models the world OTTO-Q does not control —
when vehicles come back and how much energy they need — and deliberately stops
short of deciding anything about how to serve that demand (that is the solver's
and the deterministic core's job, downstream).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from app.forecasters.priors import Distribution, Priors, Profile

# ----------------------------------------------------------------------------
# Provenance — every number carries its source back to a real-world dataset
# ----------------------------------------------------------------------------


def _prov(priors: Priors, dataset: str, variable: str = "",
          units: str = "") -> dict:
    meta = priors.datasets[dataset]
    return {
        "dataset_code": dataset,
        "source_name": meta.source_name,
        "source_org": meta.source_org,
        "source_url": meta.source_url,
        "domain": meta.domain,
        "variable": variable,
        "units": units,
        "snapshot_fingerprint": priors.fingerprint,
    }


# ----------------------------------------------------------------------------
# Quantile machinery over the 101-point grids
# ----------------------------------------------------------------------------


#: The standard normal 90th percentile, for the fleet aggregate band (L-36).
NORMAL_Z_P90 = 1.2816

#: Below this fleet size the CLT normal approximation to the n-fold convolution
#: is not tight, and the basis block says so rather than the caller guessing.
CLT_MIN_FLEET = 30


def _grid_quantile(grid: list[float], p: float) -> float:
    """Value at probability p, linear interpolation over the 101-point grid
    (grid[i] is the value at quantile i/100)."""
    if p <= 0.0:
        return float(grid[0])
    if p >= 1.0:
        return float(grid[-1])
    x = p * 100.0
    lo = int(x)
    hi = min(lo + 1, 100)
    frac = x - lo
    return float(grid[lo]) + frac * (float(grid[hi]) - float(grid[lo]))


def _poisson_quantile(lam: float, p: float) -> int:
    """Smallest k with P(Poisson(lam) <= k) >= p. Iterative, exact to float
    precision; no scipy dependency."""
    if lam <= 0.0:
        return 0
    cdf = math.exp(-lam)          # P(X=0)
    k = 0
    term = cdf
    while cdf < p and k < 100000:
        k += 1
        term *= lam / k           # P(X=k) from P(X=k-1)
        cdf += term
    return k


#: THE SITE'S OWN CLOCK, and the reference instant its offset is taken at
#: (finding L-35). Every hourly_24 profile declares the timezone its hour keys
#: are bucketed in; a forecast for a SITE has to convert each of them into that
#: site's clock before combining them at the same hour index. Doing otherwise
#: is how a UTC-bucketed ACN shape and an America/Chicago EIA shape were added
#: together at the same `hod` and the EV load forecast peaked 8 hours late.
DEFAULT_SITE_TZ = "America/Chicago"          # the flagship depot, Nashville

#: STANDARD TIME, STATED. These are 24-hour CLIMATOLOGICAL shapes, not dated
#: series, so there is no single correct instant at which to take an offset. A
#: standard-time reference is the honest choice for a shape that describes "a
#: typical day", and it is off by one hour for a summer day in a DST zone. A
#: caller that needs a dated alignment passes its own `tz_reference`.
TZ_REFERENCE = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def _utc_offset_hours(tz_name: str, reference: datetime) -> float:
    """The zone's UTC offset in hours at `reference`."""
    return ZoneInfo(tz_name).utcoffset(reference).total_seconds() / 3600.0


def _at_site_hour(profile: Profile, hod: int, site_tz: str,
                  reference: datetime = TZ_REFERENCE) -> float:
    """Read an hourly_24 profile at the site's local hour `hod`.

    The profile's hour keys are in ITS basis; the site asks in ITS own. When it
    is 07:00 in Chicago it is 05:00 in Los Angeles and 13:00 in UTC, so the
    index is shifted by the difference of the two offsets — the arithmetic the
    consumer used to skip entirely by indexing every profile at the same `hod`.
    """
    if profile.profile_kind != "hourly_24":
        raise ValueError(f"{profile.dataset_code}.{profile.profile_name} is "
                         f"{profile.profile_kind}, not an hourly shape")
    shift = (_utc_offset_hours(site_tz, reference)
             - _utc_offset_hours(profile.clock_basis, reference))
    idx = int(round(hod - shift)) % 24
    return profile.data.get(str(idx), 1.0)


def _profile(priors: Priors, dataset: str, name: str) -> Profile:
    return priors.profiles[dataset][name]


def _dist(priors: Priors, dataset: str, name: str) -> Distribution:
    return priors.distributions[dataset][name]


# ----------------------------------------------------------------------------
# Arrivals
# ----------------------------------------------------------------------------


@dataclass
class ArrivalForecast:
    horizon_hours: int
    start_hour: int           # 0-23, hour of day the horizon begins at
    dow: int                  # 0=Monday .. 6=Sunday
    mean_daily_arrivals: float
    hours: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": "arrivals",
            "horizon_hours": self.horizon_hours,
            "start_hour": self.start_hour,
            "dow": self.dow,
            "mean_daily_arrivals": round(self.mean_daily_arrivals, 2),
            "hours": self.hours,
            "provenance": self.provenance,
        }


def forecast_arrivals(priors: Priors, *, fleet_size: int, turns_per_day: float,
                      horizon_hours: int = 24, start_hour: int = 0,
                      dow: int = 0,
                      site_tz: str = DEFAULT_SITE_TZ,
                      tz_reference: datetime = TZ_REFERENCE) -> ArrivalForecast:
    """Expected vehicle return count per hour.

    The SHAPE is real (NYC for-hire-vehicle pickup timestamps, 3.5M records);
    the SCALE is declared by the operator (fleet_size x turns_per_day). A
    forecast that tried to learn the scale from OTTO-Q's dispatch history would
    be learning the engine's decisions, not demand — so the scale is an input.
    """
    hourly = _profile(priors, "nyc_tlc", "hourly_arrival_rate")
    dow_mult = _profile(priors, "nyc_tlc", "dow_demand_multiplier")

    mean_daily = fleet_size * turns_per_day
    mean_hourly = mean_daily / 24.0
    dw = dow_mult.data.get(str(dow % 7), 1.0)

    out = ArrivalForecast(
        horizon_hours=horizon_hours, start_hour=start_hour, dow=dow,
        mean_daily_arrivals=mean_daily,
        provenance=_prov(priors, "nyc_tlc", "hourly_arrival_rate",
                         "vehicles/hour"),
    )
    for h in range(horizon_hours):
        hod = (start_hour + h) % 24
        lam = mean_hourly * _at_site_hour(hourly, hod, site_tz, tz_reference) * dw
        out.hours.append({
            "hour_index": h,
            "hour_of_day": hod,
            "expected_arrivals": round(lam, 3),
            #: THE CLIMATOLOGY THIS HOUR WAS EXPECTED TO BRING, emitted beside
            #: the nowcast. Here they are IDENTICAL, and that is the honest
            #: statement about this forecaster: it is pure climatology
            #: (mean_hourly x hourly_shape x dow_mult) with no live observation
            #: and no perturbation, so it can never report a deviation from
            #: itself. Consumers that test for a surge need both numbers --
            #: comparing the window against a flat daily mean instead asks "is
            #: this above the daily average?", which the diurnal shape already
            #: answers for every hour and which the shape's own peak (1.91x)
            #: cannot push past a 2.0 threshold. A nowcasting forecaster raises
            #: expected_arrivals above this field; that gap is the signal.
            "baseline_arrivals": round(lam, 3),
            "p10": _poisson_quantile(lam, 0.10),
            "p50": _poisson_quantile(lam, 0.50),
            "p90": _poisson_quantile(lam, 0.90),
        })
    return out


# ----------------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------------


@dataclass
class LoadForecast:
    horizon_hours: int
    start_hour: int
    dow: int
    base_load_kw: float
    ev_daily_sessions: float
    hours: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": "load",
            "horizon_hours": self.horizon_hours,
            "start_hour": self.start_hour,
            "dow": self.dow,
            "base_load_kw": round(self.base_load_kw, 2),
            "ev_daily_sessions": round(self.ev_daily_sessions, 2),
            "hours": self.hours,
            "provenance": self.provenance,
        }


def forecast_load(priors: Priors, *, base_load_kw: float, ev_daily_sessions: float,
                  horizon_hours: int = 24, start_hour: int = 0,
                  dow: int = 0, site_tz: str = DEFAULT_SITE_TZ,
                  tz_reference: datetime = TZ_REFERENCE) -> LoadForecast:
    """Expected site kW per hour = base load (regional grid shape) + EV charging.

    Base load follows the EIA TVA balancing-authority hourly shape (real grid
    data), scaled by the site's declared non-EV baseline.

    EV charging load is a COMPOUND POISSON: the session count per hour is
    Poisson-shaped by ACN-Data's real charging-arrival shape, and each session
    draws energy from ACN-Data's real per-session distribution. Expected energy
    per hour = lambda_h * mean_energy; the p10/p90 band is the compound-Poisson
    standard deviation (variance = lambda * (sigma^2 + mu^2)), so the band
    honestly reflects BOTH count and per-session energy uncertainty.
    """
    acn_hourly = _profile(priors, "acn_data", "hourly_charge_arrival_rate")
    acn_dow = _profile(priors, "acn_data", "dow_charge_multiplier")
    eia_shape = _profile(priors, "eia_grid", "hourly_grid_demand_shape")
    energy = _dist(priors, "acn_data", "energy_delivered_kwh")

    mu = float(energy.mean_value) if energy.mean_value is not None else 0.0
    sd = float(energy.stddev_value) if energy.stddev_value is not None else 0.0
    dw = acn_dow.data.get(str(dow % 7), 1.0)
    mean_hourly_sessions = ev_daily_sessions / 24.0

    out = LoadForecast(
        horizon_hours=horizon_hours, start_hour=start_hour, dow=dow,
        base_load_kw=base_load_kw, ev_daily_sessions=ev_daily_sessions,
        provenance=_prov(priors, "acn_data", "energy_delivered_kwh", "kW"),
    )
    for h in range(horizon_hours):
        hod = (start_hour + h) % 24
        base = base_load_kw * _at_site_hour(eia_shape, hod, site_tz, tz_reference)
        lam = (mean_hourly_sessions
               * _at_site_hour(acn_hourly, hod, site_tz, tz_reference) * dw)
        ev_expected = lam * mu
        ev_var = lam * (sd * sd + mu * mu)
        ev_std = math.sqrt(max(0.0, ev_var))
        total = base + ev_expected
        out.hours.append({
            "hour_index": h,
            "hour_of_day": hod,
            "base_kw": round(base, 2),
            "ev_kw_expected": round(ev_expected, 2),
            "total_kw_p50": round(total, 2),
            "total_kw_p10": round(max(0.0, total - 1.28 * ev_std), 2),
            "total_kw_p90": round(total + 1.28 * ev_std, 2),
        })
    return out


# ----------------------------------------------------------------------------
# SoC return + implied energy need
# ----------------------------------------------------------------------------


@dataclass
class SocReturnForecast:
    departure_soc_pct: float
    target_soc_pct: float
    battery_kwh: float
    fleet_size: int
    return_soc: dict = field(default_factory=dict)
    energy_need_per_vehicle_kwh: dict = field(default_factory=dict)
    fleet_energy_need_kwh: dict = field(default_factory=dict)
    #: How the fleet band was derived, so the number is never read as a bare
    #: quantile of something it is not (finding L-36).
    fleet_energy_need_basis: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": "soc_return",
            "departure_soc_pct": self.departure_soc_pct,
            "target_soc_pct": self.target_soc_pct,
            "battery_kwh": self.battery_kwh,
            "fleet_size": self.fleet_size,
            "return_soc_pct": self.return_soc,
            "energy_need_per_vehicle_kwh": self.energy_need_per_vehicle_kwh,
            "fleet_energy_need_kwh": self.fleet_energy_need_kwh,
            "fleet_energy_need_basis": self.fleet_energy_need_basis,
            "provenance": self.provenance,
        }


def forecast_soc_return(priors: Priors, *, departure_soc_pct: float,
                        target_soc_pct: float, battery_kwh: float,
                        fleet_size: int) -> SocReturnForecast:
    """Distribution of state-of-charge at return, and the energy need it implies.

    Energy consumed per vehicle per day is drawn from NREL Fleet DNA's real
    daily-energy distribution. Return SoC = departure SoC minus that energy as a
    fraction of battery capacity. The energy needed to restore each vehicle to
    target is the demand the agentic layer actually cares about — this is the
    "how tight is tonight" number, and it is demand-side only (no decision made
    here about how to serve it).
    """
    energy_dist = _dist(priors, "nrel_fleet", "daily_energy_kwh")

    def _soc(energy_kwh: float) -> float:
        drop = energy_kwh / battery_kwh * 100.0
        return max(0.0, min(100.0, departure_soc_pct - drop))

    def _need(energy_kwh: float) -> float:
        """Energy to lift a vehicle from its return SoC back to target."""
        soc = _soc(energy_kwh)
        return max(0.0, (target_soc_pct - soc) / 100.0 * battery_kwh)

    grid = energy_dist.quantile_grid
    e_p10 = _grid_quantile(grid, 0.10)
    e_p50 = _grid_quantile(grid, 0.50)
    e_p90 = _grid_quantile(grid, 0.90)

    out = SocReturnForecast(
        departure_soc_pct=departure_soc_pct,
        target_soc_pct=target_soc_pct,
        battery_kwh=battery_kwh,
        fleet_size=fleet_size,
        provenance=_prov(priors, "nrel_fleet", "daily_energy_kwh", "kWh"),
    )
    out.return_soc = {
        "p10": round(_soc(e_p90), 1),   # low energy consumed -> high return SoC
        "p50": round(_soc(e_p50), 1),
        "p90": round(_soc(e_p10), 1),   # high energy consumed -> low return SoC
    }
    out.energy_need_per_vehicle_kwh = {
        "p10": round(_need(e_p10), 1),
        "p50": round(_need(e_p50), 1),
        "p90": round(_need(e_p90), 1),
    }
    #: THE FLEET BAND IS A CONVOLUTION, NOT A SCALING (finding L-36).
    #:
    #: This was `_need(e_p) * fleet_size` for each p, which says "every vehicle
    #: simultaneously at its own 90th percentile" -- an event whose probability
    #: is ~0, not the 90th percentile of the fleet total. For n independent
    #: vehicles the aggregate band narrows as sqrt(n), so at n = 118 the
    #: published band was about 3.7x too wide in BOTH directions. The p50 was
    #: wrong too, and for a separate reason: `_need` is `max(0, ...)`, clamped
    #: and therefore nonlinear, so the sum of medians is not the median of the
    #: sum. This is the number the module docstring calls "the 'how tight is
    #: tonight' number" -- the headline output of the whole forecast.
    #:
    #: The correct aggregate is the n-fold convolution of the per-vehicle need
    #: distribution. The cheap exact-enough form, given the committed 101-point
    #: grid: take mu and sigma^2 of `_need` numerically over the grid (the grid
    #: IS an equally-weighted sample of the distribution, one pass each), then
    #: report n*mu -/+ z*sigma*sqrt(n). Both moments come from the clamped
    #: function, so the clamp is handled rather than stepped around.
    #:
    #: The normal approximation is the CLT, and it is labelled as such: it is
    #: within ~1% of Monte-Carlo for n >= 30 and degrades below that, so the
    #: basis block carries the method and the threshold instead of the caller
    #: having to know.
    needs = [_need(float(x)) for x in grid]
    mu = sum(needs) / len(needs)
    var = sum((v - mu) ** 2 for v in needs) / len(needs)
    fleet_mean = mu * fleet_size
    fleet_sd = math.sqrt(var * fleet_size)
    out.fleet_energy_need_kwh = {
        #: clamped at zero: a fleet cannot need negative energy, and the normal
        #: tail can reach below zero for a small fleet with a wide per-vehicle
        #: spread.
        "p10": round(max(0.0, fleet_mean - NORMAL_Z_P90 * fleet_sd), 1),
        "p50": round(fleet_mean, 1),
        "p90": round(fleet_mean + NORMAL_Z_P90 * fleet_sd, 1),
    }
    out.fleet_energy_need_basis = {
        "method": "clt_normal_convolution",
        "per_vehicle_mean_kwh": round(mu, 3),
        "per_vehicle_stddev_kwh": round(math.sqrt(var), 3),
        "z_p90": NORMAL_Z_P90,
        "valid_for_fleet_size_at_least": CLT_MIN_FLEET,
        "note": ("fleet p10/p90 are quantiles of the FLEET TOTAL, narrowing as "
                 "sqrt(n); they are not the per-vehicle quantiles scaled, which "
                 "would describe every vehicle hitting its own tail at once"),
        #: STATED, NOT ASSUMED SILENTLY: sqrt(n) narrowing is the independent
        #: case, which is the model this forecast already uses (each vehicle's
        #: daily energy is drawn from the same distribution independently). A
        #: fleet whose duty cycles are correlated -- one depot, one shift
        #: pattern -- has a WIDER true band than this, and the honest place to
        #: fix that is the per-vehicle distribution, not a fudge factor here.
        "assumes": "vehicles independent (sqrt(n) narrowing)",
    }
    if fleet_size < CLT_MIN_FLEET:
        out.fleet_energy_need_basis["caveat"] = (
            f"fleet_size {fleet_size} is below {CLT_MIN_FLEET}: the normal "
            f"approximation to the {fleet_size}-fold convolution is not tight "
            f"here, and the band should be read as indicative")
    return out


# ----------------------------------------------------------------------------
# Composite entry point
# ----------------------------------------------------------------------------


def forecast(priors: Priors, *, fleet_size: int, turns_per_day: float,
             base_load_kw: float, ev_daily_sessions: float,
             departure_soc_pct: float, target_soc_pct: float,
             battery_kwh: float, horizon_hours: int = 24, start_hour: int = 0,
             dow: int = 0,
             site_tz: str = DEFAULT_SITE_TZ,
             tz_reference: datetime = TZ_REFERENCE) -> dict:
    """The full demand-side forecast as one dict — what `/forecast` returns."""
    return {
        "forecast_generated_at": priors.generated_at,
        "priors_fingerprint": priors.fingerprint,
        #: WHICH CLOCK THE HOURS ARE IN. Every hour_of_day in this response is
        #: the SITE's local hour, and each prior shape was converted from its
        #: own declared basis to reach it (finding L-35).
        "site_tz": site_tz,
        "arrivals": forecast_arrivals(
            priors, fleet_size=fleet_size, turns_per_day=turns_per_day,
            horizon_hours=horizon_hours, start_hour=start_hour, dow=dow,
            site_tz=site_tz, tz_reference=tz_reference,
        ).to_dict(),
        "load": forecast_load(
            priors, base_load_kw=base_load_kw,
            ev_daily_sessions=ev_daily_sessions, horizon_hours=horizon_hours,
            start_hour=start_hour, dow=dow,
            site_tz=site_tz, tz_reference=tz_reference,
        ).to_dict(),
        "soc_return": forecast_soc_return(
            priors, departure_soc_pct=departure_soc_pct,
            target_soc_pct=target_soc_pct, battery_kwh=battery_kwh,
            fleet_size=fleet_size,
        ).to_dict(),
    }
