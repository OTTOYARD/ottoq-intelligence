"""
OTTO-Q Energy Optimizer — rolling-horizon MPC (peak-shaving + TOU + degradation).

Replaces the deterministic "discharge-to-target / recharge-in-valleys" heuristic
(ottoq_energy_orchestrate cert_02/cert_03) with a real optimization that, given a
load + solar + price forecast over a horizon, computes the BESS discharge/charge
schedule that MINIMIZES:

    demand_charge_$/kW * monthly_billing_peak      (the ratchet — the big lever)
  + sum_t  energy_price_t * grid_import_t * dt     (TOU / LMP energy cost)
  + degradation_$/kWh * battery_throughput         (battery wear)

subject to BESS dynamics (SoC, round-trip efficiency), power/energy limits, and the
demand-charge RATCHET (peak >= the peak already set this billing period).

This is a linear program (the peak is captured by `peak >= grid_t` for all t), so it
solves in milliseconds with HiGHS. The twin runs it MPC-style: re-solve each tick,
apply setpoint[0], roll forward. A `robust` mode inflates the forecast by an
uncertainty band (from the probabilistic forecaster, FR-2) for chance-constrained
peak protection.

Solver: HiGHS (via PuLP if available, else CBC). Swap-in cuOpt LP for GPU scale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pulp


@dataclass
class BessState:
    soc_kwh: float                      # current energy in the battery
    capacity_kwh: float
    soc_floor_frac: float = 0.10        # reserve floor (never discharge below)
    soc_ceiling_frac: float = 0.95
    max_charge_kw: float = 1500.0
    max_discharge_kw: float = 1500.0
    eff_charge: float = 0.95            # one-way efficiencies
    eff_discharge: float = 0.95
    degradation_usd_per_kwh: float = 0.02   # throughput wear ($/kWh moved)


@dataclass
class EnergyOptResult:
    bess_setpoint_kw: List[float]       # +discharge / -charge per step; twin applies [0]
    grid_import_kw: List[float]
    soc_kwh: List[float]
    predicted_peak_kw: float
    objective_usd: float
    demand_cost_usd: float
    energy_cost_usd: float
    degradation_usd: float
    dr_violation_kwh: float             # energy drawn above the DR/feeder cap (0 if honored)
    total_bill_usd: float               # demand + energy + degradation (the economic bottom line)
    status: str
    solver: str


def _pick_solver(msg: bool = False):
    """Prefer HiGHS (fast, modern, OSS); fall back to CBC bundled with PuLP."""
    # PuLP >= 2.8 exposes HiGHS; guard so the module works anywhere.
    for name in ("HiGHS", "HiGHS_CMD"):
        cls = getattr(pulp, name, None)
        if cls is not None:
            try:
                s = cls(msg=msg)
                if s.available():
                    return s, name
            except Exception:
                pass
    return pulp.PULP_CBC_CMD(msg=msg), "CBC"


def optimize_energy(
    load_kw: List[float],               # forecasted site load (base + EV charging) per step
    solar_kw: List[float],              # forecasted solar generation per step
    energy_price_usd_per_kwh: List[float],
    tick_hours: float,                  # step duration in hours (e.g. 0.5 for 30-min ticks)
    bess: BessState,
    demand_charge_usd_per_kw: float,
    billing_period_peak_kw: float = 0.0,   # ratchet: the monthly 15-min peak already set
    end_soc_min_frac: Optional[float] = None,  # optionally require the battery re-charged by horizon end
    allow_export: bool = False,
    uncertainty_kw: Optional[List[float]] = None,  # robust band per step (from FR-2 forecaster)
    grid_cap_kw: Optional[List[float]] = None,     # demand-response / feeder ceiling per step (None = no cap)
    dr_penalty_usd_per_kwh: float = 5.0,           # penalty for drawing above the DR/feeder cap
    solver_msg: bool = False,
) -> EnergyOptResult:
    T = len(load_kw)
    assert len(solar_kw) == T and len(energy_price_usd_per_kwh) == T, "forecast length mismatch"
    if uncertainty_kw is None:
        uncertainty_kw = [0.0] * T

    floor = bess.soc_floor_frac * bess.capacity_kwh
    ceil = bess.soc_ceiling_frac * bess.capacity_kwh

    prob = pulp.LpProblem("ottoq_energy_mpc", pulp.LpMinimize)

    dis = [pulp.LpVariable(f"dis_{t}", 0, bess.max_discharge_kw) for t in range(T)]
    chg = [pulp.LpVariable(f"chg_{t}", 0, bess.max_charge_kw) for t in range(T)]
    soc = [pulp.LpVariable(f"soc_{t}", floor, ceil) for t in range(T)]
    grid = [pulp.LpVariable(f"grid_{t}", (None if allow_export else 0)) for t in range(T)]
    peak = pulp.LpVariable("peak", lowBound=max(0.0, billing_period_peak_kw))
    # DR/feeder cap as a SOFT constraint: viol[t] = grid drawn above the cap. Soft (not hard)
    # so the LP always returns the least-violating schedule when the cap is physically
    # infeasible, and the twin never has to hold vehicles to honor a grid limit.
    viol = [pulp.LpVariable(f"viol_{t}", 0) for t in range(T)]

    for t in range(T):
        # site energy balance: grid import covers load minus solar minus battery discharge plus charge
        prob += grid[t] == load_kw[t] - solar_kw[t] - dis[t] + chg[t], f"balance_{t}"
        # demand-charge peak: robust band protects against forecast under-estimate
        prob += peak >= grid[t] + uncertainty_kw[t], f"peak_{t}"
        prev = bess.soc_kwh if t == 0 else soc[t - 1]
        prob += soc[t] == prev + (chg[t] * bess.eff_charge - dis[t] / bess.eff_discharge) * tick_hours, f"soc_{t}"
        cap_t = None if grid_cap_kw is None else grid_cap_kw[t]
        if cap_t is not None and cap_t < float("inf"):
            prob += grid[t] - viol[t] <= cap_t, f"drcap_{t}"

    if end_soc_min_frac is not None:
        prob += soc[T - 1] >= end_soc_min_frac * bess.capacity_kwh, "end_soc"

    demand_cost = demand_charge_usd_per_kw * peak
    energy_cost = pulp.lpSum(energy_price_usd_per_kwh[t] * grid[t] * tick_hours for t in range(T))
    degradation = pulp.lpSum(bess.degradation_usd_per_kwh * (dis[t] + chg[t]) * tick_hours for t in range(T))
    dr_cost = pulp.lpSum(dr_penalty_usd_per_kwh * viol[t] * tick_hours for t in range(T))
    prob += demand_cost + energy_cost + degradation + dr_cost

    solver, solver_name = _pick_solver(msg=solver_msg)
    prob.solve(solver)

    status = pulp.LpStatus[prob.status]
    setpoint = [float((dis[t].value() or 0.0) - (chg[t].value() or 0.0)) for t in range(T)]
    grid_v = [float(grid[t].value() or 0.0) for t in range(T)]
    soc_v = [float(soc[t].value() or 0.0) for t in range(T)]
    peak_v = float(peak.value() or 0.0)
    dr_viol_kwh = float(sum((viol[t].value() or 0.0) * tick_hours for t in range(T)))

    demand_usd = demand_charge_usd_per_kw * peak_v
    energy_usd = float(sum(energy_price_usd_per_kwh[t] * grid_v[t] * tick_hours for t in range(T)))
    degr_usd = float(sum(bess.degradation_usd_per_kwh * (abs(setpoint[t])) * tick_hours for t in range(T)))

    return EnergyOptResult(
        bess_setpoint_kw=setpoint,
        grid_import_kw=grid_v,
        soc_kwh=soc_v,
        predicted_peak_kw=peak_v,
        objective_usd=float(pulp.value(prob.objective) or 0.0),
        demand_cost_usd=demand_usd,
        energy_cost_usd=energy_usd,
        degradation_usd=degr_usd,
        dr_violation_kwh=dr_viol_kwh,
        total_bill_usd=demand_usd + energy_usd + degr_usd,
        status=status,
        solver=solver_name,
    )
