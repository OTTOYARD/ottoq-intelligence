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

#: THE IMPORT GUARD IS AN ALLOWLIST, AND THAT IS THE WHOLE POINT.
#: It used to be a blacklist of eleven third-party names (sqlalchemy, supabase,
#: psycopg2, requests, httpx, aiohttp, boto3, asyncpg, pymongo, redis). Every
#: other route to a socket or a database walked straight past it: urllib.request,
#: socket, http.client and ftplib are STDLIB and were not on the list; sqlite3 is
#: a database that was not on the list; subprocess can shell out to psql; and
#: os.environ can carry a DSN. A blacklist can only forbid what its author
#: thought of, and the thing being guarded here -- that the forecast cannot see
#: OTTO-Q's own decisions -- is exactly the kind of claim that must not depend on
#: an author's imagination.
#: An allowlist inverts that: a new import is refused until somebody adds it here
#: deliberately, in a diff a reviewer sees.
ALLOWED_IMPORTS = {
    "__future__", "math", "json", "hashlib", "pathlib", "dataclasses",
    "typing", "collections", "itertools", "functools", "enum", "decimal",
    "app",  # only app.forecasters.* — enforced separately below
}

#: Ways to reach a module without an import statement. All are ast.Call nodes,
#: so the import guard above never saw them.
FORBIDDEN_CALLS = {"__import__", "eval", "exec", "compile", "open"}


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


def _package_sources():
    """Every .py in the package, RECURSIVELY.

    Both guards used PKG.glob("*.py"), which is top-level only. A module in a
    subdirectory of app/forecasters/ was never parsed at all: it could import a
    database driver, carry a DSN, name a forbidden table and feed a
    decision-derived number into the forecast, and statistical.py could import it
    with a plain `from app.forecasters.sub import x` that the ImportFrom branch
    waved through as an `app` import. rglob closes that.
    """
    return sorted(PKG.rglob("*.py"))


def test_contamination_guard_no_db_or_network_import():
    for py in _package_sources():
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    root = a.name.split(".")[0]
                    assert root in ALLOWED_IMPORTS, \
                        f"T1 FAIL: {py.name} imports {a.name} (not on the allowlist)"
                    assert not a.name.startswith("app.") or a.name.startswith("app.forecasters"), \
                        f"T1 FAIL: {py.name} imports {a.name} from outside app.forecasters"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                #: a relative import (level > 0) stays inside the package
                if node.level:
                    continue
                root = mod.split(".")[0]
                assert root in ALLOWED_IMPORTS, \
                    f"T1 FAIL: {py.name} imports from {mod} (not on the allowlist)"
                assert not mod.startswith("app.") or mod.startswith("app.forecasters"), \
                    f"T1 FAIL: {py.name} imports from {mod}, outside app.forecasters"


def test_contamination_guard_no_dynamic_import_or_eval():
    """__import__('psycopg2') is a Call, not an Import, and the old guard was blind to it."""
    for py in _package_sources():
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            assert name not in FORBIDDEN_CALLS, \
                f"T1 FAIL: {py.name} calls {name}() — a route to code or files the import guard cannot see"
            if isinstance(fn, ast.Attribute) and name == "import_module":
                raise AssertionError(f"T1 FAIL: {py.name} calls importlib.import_module()")


def test_contamination_guard_the_whole_imported_graph_stays_in_the_package():
    """Import the package for real and check what THAT import dragged in.

    The AST guards read files; this one reads sys.modules. A first-party module
    outside app/forecasters that itself imports a database driver would satisfy
    every textual check and still put the driver in the process. Measured as a
    before/after diff around the import, so the test file, pytest and the stdlib
    already loaded are not mistaken for the package's own dependencies.
    """
    import sys
    import importlib
    repo = PKG.parents[1].resolve()
    pkg = PKG.resolve()
    for m in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
        del sys.modules[m]
    before = set(sys.modules)
    importlib.import_module("app.forecasters")
    pulled_in = set(sys.modules) - before

    offenders = []
    for name in sorted(pulled_in):
        f = getattr(sys.modules[name], "__file__", None)
        if not f:
            continue
        fp = Path(f).resolve()
        try:
            fp.relative_to(repo)
        except ValueError:
            continue  # stdlib / site-packages: the allowlist above governs these
        #: app/__init__.py is the parent package Python must import to reach
        #: app.forecasters at all; it is empty and is allowed.
        if fp == (pkg.parent / "__init__.py"):
            continue
        if pkg not in fp.parents and fp.parent != pkg:
            offenders.append(name)
    assert not offenders, \
        f"T1 FAIL: importing app.forecasters pulled in first-party modules outside it: {offenders}"


def test_contamination_guard_no_sim_identifiers_in_code():
    """Match the forbidden names wherever they can hide, not only as bare Names.

    The scan used to look at ast.Name and whole string Constants only, so an
    attribute (`db.ottoq_decisions`), a keyword argument (`table=...`), a
    function or class NAME, a bytes literal, and an f-string piece all slipped
    through. Every node that can carry an identifier is now checked.
    """
    for py in _package_sources():
        tree = ast.parse(py.read_text())
        skip = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            texts = []
            if isinstance(node, ast.Name):
                texts.append(node.id)
            elif isinstance(node, ast.Attribute):
                texts.append(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                texts.append(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                texts.append(node.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                texts.append(node.value)
            elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
                texts.append(node.value.decode("utf-8", "ignore"))
            for t in texts:
                assert not any(w in t for w in FORBIDDEN_IDENTS), \
                    f"T1 FAIL: {py.name} carries {t!r}"


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


def test_every_arrivals_hour_carries_its_own_climatological_baseline():
    """The consumer needs two numbers to call a surge: what it expects now, and
    what this hour was expected to bring. Emitting only the first forced the
    consumer to reconstruct the second from a flat daily mean — and against a
    diurnal shape that comparison is unreachable by construction, so
    demand_surge could never fire.

    This forecaster is pure climatology, so the two are equal here and it
    correctly never reports a surge. The field exists so that fact is visible
    and so a nowcasting forecaster has somewhere to put the real number.
    """
    a = forecast_arrivals(PRIORS, fleet_size=118, turns_per_day=2.5, dow=0)
    for h in a.hours:
        assert "baseline_arrivals" in h, f"T4 FAIL: no baseline at {h}"
        assert h["baseline_arrivals"] == h["expected_arrivals"], (
            "T4 FAIL: this forecaster has no live observation and no "
            "perturbation, so its nowcast IS its climatology")


def test_the_diurnal_shape_cannot_reach_the_surge_threshold_against_a_flat_mean():
    """Why the baseline field had to exist, measured on the SHIPPED priors.

    demand_surge's threshold is 2.0x. Against a flat daily mean the achievable
    ratio is bounded by the shape's own busiest window times the largest
    day-of-week multiplier — 1.5956 x 1.1980 = 1.9115. Below 2.0, for every
    site, hour and weekday, and scale-invariant so no fleet size rescues it.
    """
    shape = PRIORS.profiles["nyc_tlc"]["hourly_arrival_rate"].data
    dow = PRIORS.profiles["nyc_tlc"]["dow_demand_multiplier"].data
    W = 3
    best = max(sum(float(shape[str((start + i) % 24)]) for i in range(W))
               for start in range(24))
    ceiling = (best / W) * max(float(v) for v in dow.values())
    assert round(ceiling, 4) == 1.9115, f"T4 FAIL: ceiling moved to {ceiling}"
    assert ceiling < 2.0


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
    """The loader must REFUSE a tampered snapshot, not merely carry a hash.

    This test used to read `raw["manifest"]["fingerprint_md5"]` and compare it to
    `PRIORS.fingerprint` — which load_priors() had just assigned FROM that very
    field. It compared the manifest to itself. It passed with the loader's
    verification deleted, and it passed on a snapshot whose numbers had been
    edited, which is precisely the case it was named for.

    The real claim is behavioural: change a prior and the loader raises. So the
    test changes one and asserts it does.
    """
    assert PRIORS.fingerprint, "T5 FAIL: no fingerprint"
    raw = json.loads((PKG / "priors_snapshot.json").read_text())
    assert raw["manifest"]["fingerprint_md5"] == PRIORS.fingerprint

    import shutil, tempfile
    from app.forecasters.priors import load_priors as _load
    with tempfile.TemporaryDirectory() as d:
        tampered = Path(d) / "priors_snapshot.json"
        doc = json.loads((PKG / "priors_snapshot.json").read_text())
        #: move ONE number and leave the manifest hash untouched — the exact
        #: shape of a quietly edited prior set
        ds = sorted(doc["distributions"])[0]
        metric = sorted(doc["distributions"][ds])[0]
        grid = doc["distributions"][ds][metric]["quantile_grid"]
        grid[0] = float(grid[0]) + 1.0
        tampered.write_text(json.dumps(doc))
        try:
            _load(tampered)
        except ValueError as e:
            assert "fingerprint mismatch" in str(e), f"T5 FAIL: wrong refusal: {e}"
        else:
            raise AssertionError(
                "T5 FAIL: the loader accepted a snapshot whose numbers do not "
                "match its own fingerprint")

        #: and a snapshot that is merely truncated must not sail through either
        short = Path(d) / "short.json"
        doc2 = json.loads((PKG / "priors_snapshot.json").read_text())
        doc2["distributions"].pop(sorted(doc2["distributions"])[0])  # drop a whole dataset
        short.write_text(json.dumps(doc2))
        try:
            _load(short)
        except (ValueError, KeyError):
            pass
        else:
            raise AssertionError("T5 FAIL: the loader accepted a truncated snapshot")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"{fn.__name__} PASS")
    print("ALL FORECAST TESTS PASS")
