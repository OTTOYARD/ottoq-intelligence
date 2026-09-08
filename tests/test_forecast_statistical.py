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

from zoneinfo import ZoneInfo

import pytest

from app.forecasters.priors import (
    SNAPSHOT_PATH,
    _canonical_priors,
    fingerprint,
    load_priors,
)
from app.forecasters.priors import load_priors as _load_priors
from app.forecasters.statistical import (
    _poisson_quantile,
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
    #: datetime + zoneinfo: added 2026-09-08 for finding L-35. Every hourly_24
    #: prior declares the timezone its hour keys are bucketed in, and the
    #: forecast converts each shape into the SITE's clock before combining
    #: them — which needs real zone offsets, not a hand-rolled table. Both are
    #: stdlib, pure, offline: zoneinfo reads the system tzdata files and opens
    #: no socket, so neither weakens what this guard exists to prove (the
    #: forecast cannot reach a database or the network, and therefore cannot
    #: learn from OTTO-Q's own decisions).
    "datetime", "zoneinfo",
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


def test_the_uncertainty_band_has_NON_ZERO_WIDTH_where_it_should():
    """Monotonicity alone certifies nothing: `p10 <= p50 <= p90` is satisfied by
    three identical numbers, so a band that collapsed to a point — the whole
    uncertainty machinery silently returning its own mean — passes the ordering
    test. Nothing anywhere asserted a band was actually WIDE, or pinned a single
    quantile to a value.

    That leaves three separate mechanisms unguarded at once: `_poisson_quantile`,
    the 1.28-sigma load band, and the per-hour lambda that feeds both.
    """
    a = forecast_arrivals(PRIORS, fleet_size=118, turns_per_day=2.5, dow=0)
    busy = [h for h in a.hours if h["expected_arrivals"] >= 2.0]
    assert busy, "T4 FAIL: no hour busy enough to carry a band; fixture is wrong"
    assert any(h["p90"] > h["p50"] > h["p10"] for h in busy), (
        "T4 FAIL: every arrivals band has zero width -- the quantiles collapsed "
        "to a point and the monotonicity test could not tell")

    l = forecast_load(PRIORS, base_load_kw=120, ev_daily_sessions=90)
    assert any(h["total_kw_p90"] > h["total_kw_p50"] > h["total_kw_p10"]
               for h in l.hours), (
        "T4 FAIL: the 1.28-sigma load band has zero width everywhere")


def test_the_quantile_function_is_pinned_to_golden_values():
    """One collapsed band is a bug the test above catches; a band that is merely
    WRONG is not. These are computed values, regenerable from the function's own
    definition (smallest k with P(Poisson(lam) <= k) >= p), and they pin the
    arithmetic itself.

    The lam <= 0 case is the one place a zero-width band is CORRECT: an hour
    that expects nothing has no uncertainty to express.
    """
    assert _poisson_quantile(2.5, 0.10) == 1
    assert _poisson_quantile(2.5, 0.50) == 2
    assert _poisson_quantile(2.5, 0.90) == 5
    assert _poisson_quantile(10.0, 0.90) == 14
    assert _poisson_quantile(0.0, 0.90) == 0, (
        "an hour expecting nothing must have a degenerate band, not a guess")


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
    #: THE OLD ASSERTION PINNED THE BUG (finding L-36): it required the fleet
    #: band to be the per-vehicle band times fleet_size, which is exactly the
    #: error -- "every vehicle simultaneously at its own tail", an event of
    #: probability ~0, rather than the quantile of the fleet TOTAL. What must
    #: hold instead is that the fleet band narrows as sqrt(n) relative to the
    #: scaled per-vehicle band, and that the centre is the mean of the clamped
    #: need (the sum of medians is not the median of the sum).
    b = s.fleet_energy_need_basis
    assert b["method"] == "clt_normal_convolution"
    assert s.fleet_energy_need_kwh["p50"] == round(
        b["per_vehicle_mean_kwh"] * s.fleet_size, 1)


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


# ---------------------------------------------------------------------------
# L-33 / L-34 / L-54: the priors snapshot's identity, content and bounds.
# ---------------------------------------------------------------------------

#: THE AUTHORITY LIVES OUTSIDE THE ARTIFACT (finding L-33).
#:
#: load_priors reads the expected hash out of the same JSON blob it is hashing,
#: which detects truncation and accidental corruption and nothing more: any
#: edit that also recomputes fingerprint() -- eleven lines, exported from the
#: same module -- is accepted silently while the `datasets` block still declares
#: ACN / TLC / EIA / NREL and every /forecast response still stamps
#: provenance.source_name as if the number came from the public dataset.
#:
#: Pinning the value HERE makes a prior change a reviewed diff. It is expected
#: to move when the priors are genuinely re-pulled; moving it is then a
#: deliberate line in a commit, which is the whole point.
EXPECTED_PRIORS_FINGERPRINT = "e3262decaae84317737dfd71817437e7"

#: What the engine's own content hash said at pull time. Recorded in the
#: snapshot manifest so a snapshot can be checked against the source it claims
#: to come from, rather than only against itself.
EXPECTED_ENGINE_FINGERPRINT = "11a246262ff7a2c929483b1ee0a7cd2d"


def test_the_priors_fingerprint_is_pinned_outside_the_artifact():
    assert PRIORS.fingerprint == EXPECTED_PRIORS_FINGERPRINT, (
        "the priors changed. If that was deliberate, update this literal in the "
        "same commit as the snapshot — that diff is the review.")


def test_the_snapshot_records_which_engine_state_it_was_pulled_from():
    raw = json.loads(SNAPSHOT_PATH.read_text())
    m = raw["manifest"]
    assert m["engine_calibration_fingerprint"] == EXPECTED_ENGINE_FINGERPRINT
    pull = m["engine_pull"]
    assert pull["project_ref"] == "gxdrcyphqjzjsuhxuqtg"
    #: per dataset, the source's own size and span at pull time
    for code in ("acn_data", "nyc_tlc", "eia_grid", "nrel_fleet"):
        d = pull["datasets"][code]
        assert d["record_count"] > 0
        assert d["date_range_start"] < d["date_range_end"]


def test_a_refit_that_lands_the_same_numbers_does_not_change_the_identity():
    """The claim fingerprint()'s docstring makes, now true (finding L-34).

    `fitted_at` is a wall-clock timestamp copied from the engine DB and it was
    inside the hash, so a re-snapshot of NUMERICALLY IDENTICAL priors changed
    every forecast's `priors_fingerprint` — the field that identifies a
    forecast — because a refit job ran, not because a number moved.
    """
    raw = json.loads(SNAPSHOT_PATH.read_text())
    before = fingerprint(_canonical_priors(raw))
    for ds in raw["distributions"].values():
        for d in ds.values():
            d["fitted_at"] = "2099-01-01 00:00:00+00"
    assert fingerprint(_canonical_priors(raw)) == before


def test_a_changed_number_still_changes_the_identity():
    raw = json.loads(SNAPSHOT_PATH.read_text())
    before = fingerprint(_canonical_priors(raw))
    ds = next(iter(raw["distributions"].values()))
    d = next(iter(ds.values()))
    d["quantile_grid"][0] = float(d["quantile_grid"][0]) + 1.0
    assert fingerprint(_canonical_priors(raw)) != before


def test_every_numeric_prior_field_loads_as_a_number():
    """All four are JSON STRINGS in the snapshot and were assigned straight
    through into a dataclass annotated `float | None` (finding L-54)."""
    seen = 0
    for ds in PRIORS.distributions.values():
        for dist in ds.values():
            for field in ("mean_value", "stddev_value", "hard_min", "hard_max"):
                v = getattr(dist, field)
                if v is not None:
                    assert isinstance(v, float), f"{field} is {type(v).__name__}"
                    seen += 1
    assert seen > 0


def test_the_declared_bounds_are_enforced_not_decorative(tmp_path):
    """hard_min / hard_max were loaded and read by no code path at all."""
    raw = json.loads(SNAPSHOT_PATH.read_text())
    ds = next(iter(raw["distributions"].values()))
    name, d = next(iter(ds.items()))
    d["quantile_grid"][-1] = float(d["hard_max"]) + 1.0
    bad = tmp_path / "priors.json"
    raw["manifest"]["fingerprint_md5"] = fingerprint(_canonical_priors(raw))
    bad.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="hard_max"):
        _load_priors(bad)


def test_a_non_monotone_quantile_grid_is_refused(tmp_path):
    """The lookup walks the grid in order, so p90 could come back below p50 and
    every band built on it would be inverted with nothing raising."""
    raw = json.loads(SNAPSHOT_PATH.read_text())
    ds = next(iter(raw["distributions"].values()))
    _name, d = next(iter(ds.items()))
    d["quantile_grid"] = sorted((float(x) for x in d["quantile_grid"]), reverse=True)
    bad = tmp_path / "priors.json"
    raw["manifest"]["fingerprint_md5"] = fingerprint(_canonical_priors(raw))
    bad.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="monotone"):
        _load_priors(bad)


# ---------------------------------------------------------------------------
# L-36: the fleet band is a convolution, not a scaling.
# ---------------------------------------------------------------------------

def _soc(n):
    return forecast_soc_return(PRIORS, departure_soc_pct=90, target_soc_pct=80,
                               battery_kwh=90, fleet_size=n)


def test_the_fleet_band_narrows_as_sqrt_n_not_linearly():
    """The measurement, on the committed priors at the flagship fleet size.

    Before: p10 1829.0, p90 8496.0 kWh — the per-vehicle band scaled by 118.
    After:  p10 4741.1, p90 5329.1 — the quantiles of the fleet TOTAL.
    The old band was about 11x too wide; the finding estimated 3.7x, and the
    measured factor is sqrt(118) = 10.9 because the error IS the missing
    sqrt(n).
    """
    s = _soc(118)
    scaled = {p: v * 118 for p, v in s.energy_need_per_vehicle_kwh.items()}
    old_halfwidth = (scaled["p90"] - scaled["p10"]) / 2
    new_halfwidth = (s.fleet_energy_need_kwh["p90"]
                     - s.fleet_energy_need_kwh["p10"]) / 2
    assert new_halfwidth < old_halfwidth / 5, (
        f"the fleet band is still scaled, not convolved: half-width "
        f"{new_halfwidth:.0f} against the scaled {old_halfwidth:.0f}")
    assert 9 < old_halfwidth / new_halfwidth < 13, (
        "the ratio should be about sqrt(118) = 10.9")


def test_the_band_narrows_with_fleet_size():
    """The property that distinguishes a convolution from a scaling: a bigger
    fleet is MORE predictable per vehicle, not equally so."""
    def rel(n):
        s = _soc(n)
        f = s.fleet_energy_need_kwh
        return (f["p90"] - f["p10"]) / f["p50"]
    wide, narrow = rel(30), rel(3000)
    assert narrow < wide / 5, (
        f"relative band {narrow:.4f} at n=3000 vs {wide:.4f} at n=30 — it is "
        f"not narrowing as sqrt(n)")


def test_the_centre_is_the_mean_of_the_clamped_need_not_the_scaled_median():
    """`_need` is max(0, ...), so it is nonlinear and the sum of medians is not
    the median of the sum. The two differ on the committed priors."""
    s = _soc(118)
    scaled_median = s.energy_need_per_vehicle_kwh["p50"] * 118
    assert abs(s.fleet_energy_need_kwh["p50"] - scaled_median) > 100, (
        "the centre still tracks the scaled median; the clamp is being ignored")


def test_a_small_fleet_carries_the_clt_caveat():
    assert "caveat" in _soc(10).fleet_energy_need_basis
    assert "caveat" not in _soc(200).fleet_energy_need_basis


def test_the_basis_travels_with_the_number():
    d = _soc(118).to_dict() if hasattr(_soc(118), "to_dict") else None
    s = _soc(118)
    b = s.fleet_energy_need_basis
    assert b["assumes"].startswith("vehicles independent")
    assert b["z_p90"] == 1.2816
    assert b["per_vehicle_stddev_kwh"] > 0


# ---------------------------------------------------------------------------
# L-35: every hourly shape declares its clock, and the site's clock is the one
# the forecast reports in.
# ---------------------------------------------------------------------------

def test_every_hourly_profile_declares_its_clock_basis():
    for code, ps in PRIORS.profiles.items():
        for name, prof in ps.items():
            if prof.profile_kind == "hourly_24":
                assert prof.clock_basis, f"{code}.{name} declares no clock_basis"
                ZoneInfo(prof.clock_basis)   # must be a real zone


def test_an_hourly_profile_without_a_basis_is_refused(tmp_path):
    raw = json.loads(SNAPSHOT_PATH.read_text())
    del raw["profiles"]["acn_data"]["hourly_charge_arrival_rate"]["clock_basis"]
    raw["manifest"]["fingerprint_md5"] = fingerprint(_canonical_priors(raw))
    bad = tmp_path / "priors.json"
    bad.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="clock_basis"):
        _load_priors(bad)


def test_the_acn_shape_is_the_canonical_workplace_curve():
    """The evidence the refit rests on.

    The committed shape peaked at h15 and troughed at h07-h10 while describing
    workplace charging at Caltech + JPL — both America/Los_Angeles. Rotating by
    -8h recovers the curve those sites actually have: everyone arrives in the
    morning and plugs in, and nothing happens overnight. A shape that peaks at
    3 p.m. and is dead at 8 a.m. is not workplace charging; it is UTC.
    """
    acn = PRIORS.profiles["acn_data"]["hourly_charge_arrival_rate"]
    assert acn.clock_basis == "America/Los_Angeles"
    by_hour = {int(k): v for k, v in acn.data.items()}
    assert max(by_hour, key=by_hour.get) == 7, "the peak is not the morning plug-in"
    assert min(by_hour, key=by_hour.get) == 2, "the trough is not the small hours"
    #: near-zero overnight, busy in the morning. The quiet window measured on
    #: the rotated shape is 23:00-02:00 (0.10, 0.07, 0.04, 0.02); 03:00 carries
    #: a 0.32 blip, so the window is what the data shows and not a round number.
    assert max(by_hour[h] for h in (23, 0, 1, 2)) < 0.2
    assert by_hour[7] > 4.0
    #: ...and the morning is an order of magnitude above the night
    assert by_hour[7] > 20 * max(by_hour[h] for h in (23, 0, 1, 2))


def test_the_load_forecast_reads_each_shape_on_the_site_clock():
    """Two shapes on two clocks were added at the same hour index.

    On a Nashville site (America/Chicago): the EIA base shape is already
    Central and must not move, while the ACN shape must be read two hours
    earlier — 07:00 Pacific is 09:00 Central — so the EV peak lands at 09:00.
    Before the fix the raw UTC index was read as local and the EV peak sat at
    15:00, six hours off on this site's clock and eight off the Pacific curve.
    """
    l = forecast_load(PRIORS, base_load_kw=120, ev_daily_sessions=90)
    ev = {h["hour_of_day"]: h["ev_kw_expected"] for h in l.hours}
    base = {h["hour_of_day"]: h["base_kw"] for h in l.hours}
    assert max(ev, key=ev.get) == 9, f"EV peak at {max(ev, key=ev.get)}, not 09:00"
    assert max(base, key=base.get) == 18, "the Central grid shape moved"


def test_moving_the_site_moves_the_shapes_with_it():
    """A Los Angeles site sees the ACN peak at its own 07:00, unshifted."""
    la = forecast_load(PRIORS, base_load_kw=120, ev_daily_sessions=90,
                       site_tz="America/Los_Angeles")
    ev = {h["hour_of_day"]: h["ev_kw_expected"] for h in la.hours}
    assert max(ev, key=ev.get) == 7


def test_the_response_says_which_clock_its_hours_are_in():
    f = forecast(PRIORS, fleet_size=100, turns_per_day=6, base_load_kw=120,
                 ev_daily_sessions=90, departure_soc_pct=90,
                 target_soc_pct=80, battery_kwh=90)
    assert f["site_tz"] == "America/Chicago"
