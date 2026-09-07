"""Statistical forecast tests — the contamination guard and the physics.

Run:  python3 -m pytest tests/test_forecast_statistical.py -q

Every assertion here is about the forecast being (a) structurally incapable of
learning from OTTO-Q decision/sim output, (b) deterministic, (c) provenance-
carrying, and (d) physically sane. The values are not pinned to a golden number
yet — they pin the STRUCTURE, which is the thing a reviewer can attack.
"""

import ast
import json
import math
from pathlib import Path

from app.forecasters.priors import load_priors
from app.forecasters.statistical import (
    forecast, forecast_arrivals, forecast_load, forecast_soc_return,
)

PRIORS = load_priors()
PKG = Path(__file__).parent.parent / "app" / "forecasters"

# ---------------------------------------------------------------------------
# T1 — THE CONTAMINATION GUARD, STRUCTURAL.
# The forecast package must not import any database/network client and must not
# reference any sim/decision output table or object IN CODE. Documentation may
# NAME the forbidden objects (the guard docstring explains what it excludes);
# code may not touch them. Parsed via AST so docstrings/comments are excluded.
# ---------------------------------------------------------------------------

FORBIDDEN_IDENTS = [
    "ottoq_decisions", "ottoq_vehicle_dispatches", "ottoq_vehicle_commands",
    "site_energy_snapshots", "ottoq_service_detail_records",
    "ottoq_stall_bookings", "ottoq_rule_evaluations", "bess_snapshots",
    "vehicle_state_log", "ottoq_events", "sim_run_id", "data_source",
]
FORBIDDEN_IMPORTS = [
    "sqlalchemy", "supabase", "psycopg", "psycopg2", "requests", "httpx",
    "aiohttp", "boto3", "asyncpg", "pymongo", "redis",
]


def _docstring_node_ids(tree):
    """Node ids of module/class/function docstring nodes AND their Constant
    string children (so the guard skips the string content, not just the Expr
    wrapper — the docstring legitimately NAMES the forbidden tables to explain
    what the guard excludes)."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                ids.add(id(node.body[0]))
                ids.add(id(node.body[0].value))
    return ids


def test_contamination_guard_no_db_or_network_import():
    for py in PKG.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] not in FORBIDDEN_IMPORTS, \
                        f"T1 FAIL: {py.name} imports {a.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod.split(".")[0] not in FORBIDDEN_IMPORTS, \
                    f"T1 FAIL: {py.name} imports from {mod}"


def test_contamination_guard_no_sim_identifiers_in_code():
    for py in PKG.glob("*.py"):
        tree = ast.parse(py.read_text())
        skip = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_IDENTS, \
                    f"T1 FAIL: {py.name} references identifier {node.id!r}"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not any(w in node.value for w in FORBIDDEN_IDENTS), \
                    f"T1 FAIL: {py.name} carries string {node.value!r}"


def test_contamination_guard_snapshot_is_priors_only():
    snap = json.loads((PKG / "priors_snapshot.json").read_text())
    assert set(snap["datasets"]) == {"acn_data", "eia_grid", "nrel_fleet", "nyc_tlc"}
    assert snap["manifest"]["kind"] == "calibration_priors_snapshot"
    for w in FORBIDDEN_IDENTS:
        assert w not in json.dumps(snap), f"T1 FAIL: snapshot carries {w!r}"


# ---------------------------------------------------------------------------
# T2 — DETERMINISM: pure function of (priors, inputs). Two calls, byte-identical.
# ---------------------------------------------------------------------------

def test_forecast_is_deterministic():
    kw = dict(fleet_size=118, turns_per_day=2.5, base_load_kw=120,
              ev_daily_sessions=90, departure_soc_pct=90, target_soc_pct=80,
              battery_kwh=90)
    a = forecast(PRIORS, **kw)
    b = forecast(PRIORS, **kw)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), \
        "T2 FAIL: forecast is not a pure function of its inputs"


# ---------------------------------------------------------------------------
# T3 — PROVENANCE: every section traces back to a real-world dataset.
# ---------------------------------------------------------------------------

def test_provenance_on_every_section():
    f = forecast(PRIORS, fleet_size=118, turns_per_day=2.5, base_load_kw=120,
                 ev_daily_sessions=90, departure_soc_pct=90, target_soc_pct=80,
                 battery_kwh=90)
    for section in ("arrivals", "load", "soc_return"):
        prov = f[section]["provenance"]
        assert prov["dataset_code"], f"T3 FAIL: {section} missing dataset_code"
        assert prov["source_name"], f"T3 FAIL: {section} missing source_name"
        assert prov["snapshot_fingerprint"] == PRIORS.fingerprint
    codes = {f[s]["provenance"]["dataset_code"] for s in ("arrivals", "load", "soc_return")}
    assert codes == {"nyc_tlc", "acn_data", "nrel_fleet"}, \
        f"T3 FAIL: unexpected provenance set {codes}"


# ---------------------------------------------------------------------------
# T4 — PHYSICAL SANITY: shapes behave like the real world.
# ---------------------------------------------------------------------------

def test_arrivals_peak_in_evening_and_dow_scales_total():
    a = forecast_arrivals(PRIORS, fleet_size=118, turns_per_day=2.5, dow=0)
    total = sum(h["expected_arrivals"] for h in a.hours)
    dow_mult = PRIORS.profiles["nyc_tlc"]["dow_demand_multiplier"].data["0"]
    # The day-of-week multiplier intentionally scales the daily total: Monday
    # demand is 0.8423x the fleet average. The sum over 24h must equal
    # mean_daily x dow_multiplier, not the raw mean_daily.
    assert abs(total - a.mean_daily_arrivals * dow_mult) < 0.5, \
        f"T4 FAIL: total {total} != mean x dow_mult ({a.mean_daily_arrivals * dow_mult})"
    by_hod = {h["hour_of_day"]: h["expected_arrivals"] for h in a.hours}
    assert by_hod[17] > by_hod[5], "T4 FAIL: arrival shape has no evening peak"


def test_quantile_ordering_is_monotonic():
    a = forecast_arrivals(PRIORS, fleet_size=118, turns_per_day=2.5)
    for h in a.hours:
        assert h["p10"] <= h["p50"] <= h["p90"], f"T4 FAIL: non-monotonic quantiles at {h}"
    l = forecast_load(PRIORS, base_load_kw=120, ev_daily_sessions=90)
    for h in l.hours:
        assert h["total_kw_p10"] <= h["total_kw_p50"] <= h["total_kw_p90"], \
            f"T4 FAIL: non-monotonic load quantiles at {h}"


def test_soc_return_drops_below_departure_and_need_restores_target():
    s = forecast_soc_return(PRIORS, departure_soc_pct=90, target_soc_pct=80,
                            battery_kwh=90, fleet_size=100)
    assert s.return_soc["p50"] < s.departure_soc_pct, "T4 FAIL: SoC did not drop"
    assert s.return_soc["p50"] >= 0.0, "T4 FAIL: SoC went negative"
    assert s.fleet_energy_need_kwh["p50"] > 0, "T4 FAIL: fleet energy need is zero"
    # fleet need = per-vehicle need x fleet size, within per-vehicle rounding (0.1)
    assert abs(s.fleet_energy_need_kwh["p50"]
               - s.energy_need_per_vehicle_kwh["p50"] * 100) < 0.1 * 100 + 0.1, \
        "T4 FAIL: fleet need does not scale linearly with fleet size"


def test_load_base_follows_eia_shape_and_ev_is_positive():
    l = forecast_load(PRIORS, base_load_kw=120, ev_daily_sessions=90)
    by_hod = {h["hour_of_day"]: h["base_kw"] for h in l.hours}
    assert by_hod[18] > by_hod[3], "T4 FAIL: base load does not follow grid shape"
    assert all(h["ev_kw_expected"] >= 0 for h in l.hours), "T4 FAIL: negative EV load"


# ---------------------------------------------------------------------------
# T5 — SNAPSHOT INTEGRITY: fingerprint verifies.
# ---------------------------------------------------------------------------

def test_snapshot_fingerprint_verifies():
    assert PRIORS.fingerprint, "T5 FAIL: no fingerprint"
    raw = json.loads((PKG / "priors_snapshot.json").read_text())
    assert raw["manifest"]["fingerprint_md5"] == PRIORS.fingerprint, \
        "T5 FAIL: manifest fingerprint differs from loaded priors"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"{fn.__name__} PASS")
    print("ALL FORECAST TESTS PASS")
