"""The battery planner is the plan the OTTO-Q research lab measured, and it keeps the battery's limits.

Run:  python3 -m pytest tests/test_battery_planner.py -q

The fixtures are three ticks of one 36-hour lab day (otto-q-core lab/research/p2_capacity, seed 424242, 2026-11-06): the
inputs the lab's planner (battery_lp.py, OR-Tools GLOP 9.15) planned that tick on, and the objective and setpoints it
found. The lab measured that planner saving $226 a day on the depot's own day plan, on six of six seeds; this service is
how production gets it, so a plan made here must be the plan the lab made. k = 60 spans two billing periods (the lab day
is billed from 6 PM); k = 150 and 300 one.
"""
import json
import math
import os

import pytest

from app.optimizers.battery_planner import Battery, Period, plan_battery

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(k):
    with open(os.path.join(HERE, "fixtures", f"battery_planner_lab_424242_k{k}.json")) as f:
        return json.load(f)


def _plan(inp):
    return plan_battery(
        load_kw=inp["load_kw"], price_usd_kwh=inp["price_usd_kwh"], step_min=inp["step_min"],
        battery=Battery(**inp["battery"]), demand_usd_kw=inp["demand_usd_kw"],
        periods=[Period(**p) for p in inp["periods"]], past_grid_kw=inp["past_grid_kw"],
        window_steps=inp["window_steps"], soc_floor_kwh=inp["soc_floor_kwh"],
        wear_usd_kwh=inp["wear_usd_kwh"], terminal_usd_kwh=inp["terminal_usd_kwh"])


@pytest.mark.parametrize("k", [60, 150, 300])
def test_the_plan_is_the_labs_plan(k):
    fx = _load(k)
    res = _plan(fx["inputs"])
    assert res.status == "optimal"
    assert res.objective_usd == pytest.approx(fx["lab"]["objective_usd"], rel=1e-9, abs=1e-6)
    # the lab stored its setpoints to 0.1 kW
    assert max(abs(round(a, 1) - b) for a, b in zip(res.setpoint_kw, fx["lab"]["setpoint_kw"])) <= 0.1 + 1e-9
    assert res.peak_kw[-1] == pytest.approx(fx["lab"]["peak_kw"], abs=0.05 + 1e-9)


@pytest.mark.parametrize("k", [60, 150, 300])
def test_the_plan_keeps_the_batterys_limits(k):
    inp = _load(k)["inputs"]
    res = _plan(inp)
    b = inp["battery"]
    tol = 1e-6
    assert all(b["floor_kwh"] - tol <= e <= b["ceiling_kwh"] + tol for e in res.soc_kwh)
    assert all(-b["max_charge_kw"] - tol <= x <= b["max_discharge_kw"] + tol for x in res.setpoint_kw)
    assert all(g >= -tol for g in res.grid_kw)
    # the call reserve: from the second step on, the energy at a step's start is at least its floor
    for j, fl in enumerate(inp["soc_floor_kwh"]):
        if j > 0 and fl > b["floor_kwh"]:
            assert res.soc_kwh[j] >= min(b["ceiling_kwh"], fl) - tol
    for p, pk in zip([p for p in inp["periods"] if p["end"] > 0], res.peak_kw):
        assert pk >= p["sunk_kw"] - tol


def _flat(m=96, base=400.0, spike=(40, 46, 1400.0)):
    load = [base] * m
    for j in range(spike[0], spike[1]):
        load[j] = spike[2]
    return load


def test_a_demand_charge_makes_it_discharge_into_the_spike():
    load = _flat()
    res = plan_battery(load, [0.07] * len(load), 5.0, Battery(2000, 300, 2700, 1500, 1500, 0.98), demand_usd_kw=0.71,
                       periods=[Period(0, len(load))], window_steps=6, wear_usd_kwh=0.02, terminal_usd_kwh=0.07)
    assert res.status == "optimal"
    assert res.peak_kw[0] < 1400 - 500          # the spike's half hour is shaved by most of 1,000 kW
    assert all(res.setpoint_kw[j] > 0 for j in range(40, 46))


def test_the_margin_keeps_future_half_hours_under_the_peak_already_billed():
    load = [500.0] * 48
    sunk = 800.0
    res = plan_battery(load, [0.06] * 48, 5.0, Battery(300, 300, 2700, 1500, 1500, 0.98), demand_usd_kw=0.71,
                       periods=[Period(-24, 48, sunk_kw=sunk, margin_kw=100.0)], past_grid_kw=[500.0] * 5,
                       window_steps=6, wear_usd_kwh=0.02, terminal_usd_kwh=0.0672 / 0.96)
    assert res.status == "optimal"
    # charging the empty battery is worth its replacement value, but every future half hour stays 100 kW under 800
    for i0 in range(0, 48 - 6 + 1):
        assert sum(res.grid_kw[i0:i0 + 6]) / 6 <= sunk - 100 + 1e-6
    assert res.soc_kwh[-1] > 300 + 1


def test_a_solar_surplus_the_battery_cannot_take_is_spilled_not_infeasible():
    load = [-600.0] * 24                        # the sun 600 kW over the site, the battery full
    res = plan_battery(load, [0.06] * 24, 5.0, Battery(2700, 300, 2700, 1500, 1500, 0.98), demand_usd_kw=0.71,
                       periods=[Period(0, 24)], window_steps=6)
    assert res.status == "optimal"
    assert all(abs(g) < 1e-6 for g in res.grid_kw)


def test_bad_requests_are_refused():
    b = Battery(1000, 300, 2700, 1500, 1500, 0.98)
    with pytest.raises(ValueError):
        plan_battery([], [], 5.0, b, 0.7, [Period(0, 1)])
    with pytest.raises(ValueError):
        plan_battery([1.0, 2.0], [0.1], 5.0, b, 0.7, [Period(0, 2)])
    with pytest.raises(ValueError):
        plan_battery([1.0], [0.1], 5.0, Battery(1000, 300, 2700, 1500, 1500, 1.5), 0.7, [Period(0, 1)])


def test_the_endpoint_returns_the_plan():
    """The route's own function (no HTTP client: the suite installs only requirements.txt and pytest)."""
    from fastapi import HTTPException
    import app.main as m
    fx = _load(300)
    body = m.battery_plan_endpoint(m.BatteryPlanIn(**fx["inputs"]))
    assert body["status"] == "optimal"
    assert len(body["setpoint_kw"]) == len(fx["inputs"]["load_kw"])
    assert body["objective_usd"] == pytest.approx(fx["lab"]["objective_usd"], rel=1e-9, abs=1e-6)
    assert "battery_planner" in m.health()["optimizers"]
    bad = dict(fx["inputs"], price_usd_kwh=fx["inputs"]["price_usd_kwh"][:-1])
    with pytest.raises(HTTPException) as e:
        m.battery_plan_endpoint(m.BatteryPlanIn(**bad))
    assert e.value.status_code == 422


def test_step_cap_keeps_every_step_under_the_peak():
    """step_cap (production's request sets it): no step of a period draws more than the period's peak, so a plan followed
    a few steps late cannot stack charging bursts in one half hour (the lab's late-landing days, 2026-11-06)."""
    load = [300.0] * 48 + [900.0] * 12 + [300.0] * 36       # an evening wave the battery must be charged for
    kw = dict(price_usd_kwh=[0.06] * 96, step_min=5.0, battery=Battery(400, 300, 2700, 1500, 1500, 0.98),
              demand_usd_kw=0.71, periods=[Period(0, 96)], window_steps=6, wear_usd_kwh=0.02, terminal_usd_kwh=0.07)
    free = plan_battery(load, **kw)
    capped = plan_battery(load, step_cap=True, **kw)
    assert free.status == capped.status == "optimal"
    assert max(free.grid_kw) > free.peak_kw[0] + 1           # uncapped, the plan charges in bursts above its peak
    assert max(capped.grid_kw) <= capped.peak_kw[0] + 1e-6   # capped, never above it
    assert capped.peak_kw[0] <= 900 - 1                       # and the wave is still shaved
    assert capped.objective_usd >= free.objective_usd - 1e-6  # a constraint can only cost


def test_step_cap_off_is_the_labs_plan():
    fx = _load(150)
    res = plan_battery(step_cap=False, **{k: v for k, v in _kw(fx["inputs"]).items()})
    assert res.objective_usd == pytest.approx(fx["lab"]["objective_usd"], rel=1e-9, abs=1e-6)


def _kw(inp):
    return dict(load_kw=inp["load_kw"], price_usd_kwh=inp["price_usd_kwh"], step_min=inp["step_min"],
                battery=Battery(**inp["battery"]), demand_usd_kw=inp["demand_usd_kw"],
                periods=[Period(**p) for p in inp["periods"]], past_grid_kw=inp["past_grid_kw"],
                window_steps=inp["window_steps"], soc_floor_kwh=inp["soc_floor_kwh"],
                wear_usd_kwh=inp["wear_usd_kwh"], terminal_usd_kwh=inp["terminal_usd_kwh"])
