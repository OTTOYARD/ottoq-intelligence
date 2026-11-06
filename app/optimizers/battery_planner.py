"""
OTTO-Q battery planner: the depot battery's plan over a rolling horizon, as a linear program (GLOP).

This is the planner the OTTO-Q research lab measured on its twin (otto-q-core lab/research/p2_capacity/battery_lp.py,
2026-11-06): on six 36-hour lab days billed from 6 PM, the battery planned this way on a learned load forecast plus its
80th-percentile out-of-sample miss saved $226 a day on the engine's own day plan, on all six seeds, and held the billed
half hour 252-456 kW lower. The formulation here is that one, step for step, so a plan made here and a plan made in the
lab from the same inputs are the same plan (tests/test_battery_planner.py holds the parity):

  minimise   sum_j price_j * grid_j * dt                      the energy bill
           + demand_usd_kw * sum_periods peak_p                 the demand charge on each billing period's highest window
           + wear_usd_kwh * sum_j discharge_j * dt              battery wear, per kWh discharged
           - terminal_usd_kwh * energy_end                      the energy left at the end, at its replacement cost
  subject to
    energy_{j+1} = energy_j + (eff * charge_j - discharge_j / eff) * dt,   floor <= energy <= ceiling, energy_0 = now
    energy_j >= soc_floor_kwh[j] for j >= 1                     a per-step floor (the demand-response call reserve)
    grid_j = load_j + charge_j - discharge_j + spill_j >= 0     load is the site before the battery (building + EV - sun);
                                                                spill is sun the site cannot use (no export credit)
    for each billing period and every demand window (window_steps consecutive steps) not wholly in the past:
      peak_p - margin_p >= mean grid over the window when the window is wholly in the future, else peak_p >= it
    peak_p >= sunk_kw                                           the peak the period has already billed

The margin is the lab's guard: a half hour wholly in the future is planned to stay a margin under the period's peak
(the lab's first day, planned to the edge on a forecast 250-400 kW low, ratcheted its billed peak from 1,128 to 1,457
kW). The other guard (the next window never forecast under what the site drew last) belongs to whoever writes load_kw.

step_cap (production's request sets it): no step of a billing period draws more than the period's peak. The objective
bills 30-minute averages, so without it the plan is free to charge in full-power bursts inside a half hour; when the
depot follows a plan a few steps late (a plan follows the step its clock is in), bursts from successive plans stack in
the same half hour. The research lab measured it (2026-11-06): with every answer landing two ticks late, the billed
half hour rose to 1,135-1,171 kW at midday, above the day plan's.

ADVISORY, like everything this service returns. The plan reaches the depot as ottoq_energy_plan rows that the energy
orchestrator follows only while fresh and never during a demand-response call (otto-q-core 0799), inside its own limits
(the battery's power, floor, ceiling and temperature). It moves only the battery: the cars charge exactly as they would
without it. No vehicle is ever held for it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from ortools.linear_solver import pywraplp


@dataclass
class Battery:
    soc_kwh: float
    floor_kwh: float
    ceiling_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    eff_one_way: float              # sqrt(round trip)


@dataclass
class Period:
    start: int                      # first step of the billing period, relative to the plan (negative: began before it)
    end: int                        # one past its last step, relative to the plan
    sunk_kw: float = 0.0            # its highest window already wholly in the past
    margin_kw: float = 0.0          # windows wholly in the future are planned this far under its peak (the lab: max(100
                                    # kW, 10% of the sunk peak))


@dataclass
class PlanResult:
    status: str
    setpoint_kw: List[float]        # + discharge / - charge, per step; the depot follows the step its clock is in
    grid_kw: List[float]
    soc_kwh: List[float]            # energy at the start of each step, and at the end (len = steps + 1)
    peak_kw: List[float]            # each billing period's planned peak (periods wholly past omitted)
    objective_usd: Optional[float]  # None when no plan was found (status says why): JSON has no NaN
    solver: str
    solve_ms: float
    detail: dict = field(default_factory=dict)


def plan_battery(
    load_kw: List[float],
    price_usd_kwh: List[float],
    step_min: float,
    battery: Battery,
    demand_usd_kw: float,
    periods: List[Period],
    past_grid_kw: Optional[List[float]] = None,
    window_steps: int = 6,
    soc_floor_kwh: Optional[List[float]] = None,
    wear_usd_kwh: float = 0.02,
    terminal_usd_kwh: float = 0.0,
    step_cap: bool = False,
) -> PlanResult:
    m = len(load_kw)
    if m == 0:
        raise ValueError("load_kw is empty")
    if len(price_usd_kwh) != m:
        raise ValueError("price_usd_kwh and load_kw differ in length")
    if soc_floor_kwh is not None and len(soc_floor_kwh) != m:
        raise ValueError("soc_floor_kwh and load_kw differ in length")
    if window_steps < 1:
        raise ValueError("window_steps must be at least 1")
    if not (0.0 < battery.eff_one_way <= 1.0):
        raise ValueError("eff_one_way must be in (0, 1]")
    if battery.floor_kwh > battery.ceiling_kwh:
        raise ValueError("floor_kwh is above ceiling_kwh")
    dt = step_min / 60.0
    win = window_steps
    past = list(past_grid_kw or [])
    if len(past) < win - 1:                 # windows reaching before the history known count it as nothing drawn
        past = [0.0] * (win - 1 - len(past)) + past
    past = past[len(past) - (win - 1):] if win > 1 else []
    lo, hi, eff = battery.floor_kwh, battery.ceiling_kwh, battery.eff_one_way
    e0 = min(max(battery.soc_kwh, lo), hi)

    t0 = time.perf_counter()
    s = pywraplp.Solver.CreateSolver("GLOP")
    if s is None:
        raise RuntimeError("GLOP is unavailable")
    ch = [s.NumVar(0, battery.max_charge_kw, "") for _ in range(m)]
    dis = [s.NumVar(0, battery.max_discharge_kw, "") for _ in range(m)]
    e = [s.NumVar(lo, hi, "") for _ in range(m + 1)]
    g = [s.NumVar(0, s.infinity(), "") for _ in range(m)]
    sp = [s.NumVar(0, s.infinity(), "") for _ in range(m)]
    s.Add(e[0] == e0)
    for j in range(m):
        s.Add(e[j + 1] == e[j] + (eff * ch[j] - dis[j] / eff) * dt)
        s.Add(g[j] == load_kw[j] + ch[j] - dis[j] + sp[j])
        if j > 0 and soc_floor_kwh is not None and soc_floor_kwh[j] > lo:
            s.Add(e[j] >= min(hi, soc_floor_kwh[j]))
    peaks = []
    for p in periods:
        if p.end <= 0:
            continue                          # wholly past: paid, nothing left can move it
        pk = s.NumVar(0, s.infinity(), "")
        for i0 in range(max(p.start, -(win - 1)), p.end - win + 1):
            past_sum = sum(past[win - 1 + i] for i in range(i0, min(0, i0 + win)))
            fut = [g[i] for i in range(max(i0, 0), i0 + win)]
            margin = p.margin_kw if i0 >= 0 else 0.0
            s.Add(pk - margin >= (past_sum + sum(fut)) * (1.0 / win))
        s.Add(pk >= p.sunk_kw)
        if step_cap:     # no step of the period draws more than its peak: a plan followed late cannot stack bursts
            for j in range(max(p.start, 0), min(p.end, m)):
                s.Add(g[j] <= pk)
        peaks.append(pk)
    s.Minimize(sum(price_usd_kwh[j] * g[j] * dt for j in range(m))
               + demand_usd_kw * sum(peaks)
               + wear_usd_kwh * sum(dis[j] * dt for j in range(m))
               - terminal_usd_kwh * e[m])
    st = s.Solve()
    ms = (time.perf_counter() - t0) * 1000.0
    names = {pywraplp.Solver.OPTIMAL: "optimal", pywraplp.Solver.FEASIBLE: "feasible",
             pywraplp.Solver.INFEASIBLE: "infeasible", pywraplp.Solver.UNBOUNDED: "unbounded",
             pywraplp.Solver.ABNORMAL: "abnormal", pywraplp.Solver.NOT_SOLVED: "not_solved"}
    status = names.get(st, f"status_{st}")
    if st not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        # e.g. a reserve floor the battery cannot reach in time: no plan, said plainly (the caller falls back)
        return PlanResult(status, [], [], [], [], None, "glop", ms, detail={"steps": m, "e0_kwh": e0})
    return PlanResult(
        status=status,
        setpoint_kw=[dis[j].solution_value() - ch[j].solution_value() for j in range(m)],
        grid_kw=[g[j].solution_value() for j in range(m)],
        soc_kwh=[e[j].solution_value() for j in range(m + 1)],
        peak_kw=[pk.solution_value() for pk in peaks],
        objective_usd=s.Objective().Value(),
        solver="glop",
        solve_ms=ms,
        detail={"steps": m, "window_steps": win, "periods": len(peaks), "e0_kwh": e0},
    )
