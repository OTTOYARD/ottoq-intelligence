"""CP-SAT assignment adapter for the live OTTO-Q proposer seat.

The scheduling model remains owned by ``otto-q-core``. This service imports the
core bridge from a pinned checkout and exposes it over the same authenticated
FastAPI boundary used by the energy optimizer. It never writes depot state.
"""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any


CORE_ROOT = Path(os.environ.get("OTTOQ_CORE_ROOT", "/opt/otto-q-core"))
if CORE_ROOT.exists() and str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

# DO NOT NAME A CAUSE THIS HANDLER HAS NOT CHECKED.
#
# This used to raise, unconditionally, "otto-q-core is unavailable; set OTTOQ_CORE_ROOT to its
# pinned checkout". On 2026-09-21 that sentence was false and cost a day: the checkout was
# present and complete at the pinned ref, and the actual failure was an ABI collision between
# highspy's libhighs and ortools' four frames further down (see requirements.txt). The service
# then served /health with optimizers:["energy_mpc"], and the edge function's only clue was a
# 2xx it had to infer staleness from.
#
# So the handler now DISTINGUISHES the two, by looking, and carries the original message either
# way. An ImportError from deep inside a shared object is not a missing checkout, and a message
# that conflates them sends the next reader to the wrong file.
try:
    from bridge.proposer_bridge import fire
except ImportError as exc:  # pragma: no cover, exercised by deployment health
    _missing = not (CORE_ROOT / "bridge" / "proposer_bridge.py").is_file()
    raise RuntimeError(
        (
            f"otto-q-core is unavailable at OTTOQ_CORE_ROOT={CORE_ROOT}: "
            f"bridge/proposer_bridge.py is not there. Underlying: {exc!r}"
        )
        if _missing
        else (
            f"otto-q-core IS present at OTTOQ_CORE_ROOT={CORE_ROOT}, so this is NOT a missing "
            f"checkout -- the import chain itself failed. Underlying: {exc!r}"
        )
    ) from exc


OBJECTIVE_SIGNALS: dict[str, frozenset[str]] = {
    "readiness_first": frozenset(),
    "throughput_first": frozenset({"demand_surge"}),
    "energy_balanced": frozenset({"grid_peak_imminent"}),
}


def _blocked_pairs(feedback: list[dict[str, Any]]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for item in feedback:
        entity_id = item.get("entity_id")
        stall_id = item.get("stall_id")
        if isinstance(entity_id, str) and isinstance(stall_id, str):
            out.add((entity_id, stall_id))
    return out


def _conflicts(rows: list[dict[str, Any]], blocked: set[tuple[str, str]]) -> set[str]:
    stalls: set[str] = set()
    for row in rows:
        proposal = row.get("proposal") or {}
        pair = (row.get("entity_id"), proposal.get("stall_id"))
        if not proposal.get("abstain") and pair in blocked:
            stalls.add(str(pair[1]))
    return stalls


def _remove_stalls(frame: dict[str, Any], stall_ids: set[str]) -> dict[str, Any]:
    """Remove rejected resources for the next bounded solve attempt.

    Rejection feedback is used only when the solver repeats the exact rejected
    vehicle/stall pair. The retry removes that resource from the small live
    batch, forcing CP-SAT to find an alternate feasible plan or abstain.
    """
    retried = copy.deepcopy(frame)
    retried["stalls"] = [
        stall for stall in retried.get("stalls", [])
        if str(stall.get("id")) not in stall_ids
    ]
    return retried


def optimize_assignment(
    *,
    frame: dict[str, Any],
    class_rows: list[dict[str, Any]],
    site: dict[str, Any],
    sim_run_id: str,
    depot_id: str,
    objective: str,
    feedback: list[dict[str, Any]] | None = None,
    max_assets: int = 8,
    det_budget_s: float = 0.25,
    max_retries: int = 2,
    hour_of_day: int = 12,
) -> dict[str, Any]:
    if objective not in OBJECTIVE_SIGNALS:
        raise ValueError(f"unknown objective {objective!r}")
    if not 1 <= max_assets <= 24:
        raise ValueError("max_assets must be between 1 and 24")
    if not 0.01 <= det_budget_s <= 5.0:
        raise ValueError("det_budget_s must be between 0.01 and 5.0")
    if not 0 <= max_retries <= 2:
        raise ValueError("max_retries must be between 0 and 2")
    if not 0 <= hour_of_day <= 23:
        raise ValueError("hour_of_day must be between 0 and 23")

    feedback = feedback or []
    blocked = _blocked_pairs(feedback)
    working = copy.deepcopy(frame)
    attempts: list[dict[str, Any]] = []

    for attempt in range(max_retries + 1):
        result = fire(
            working,
            class_rows,
            site=site,
            sim_run_id=sim_run_id,
            depot_id=depot_id,
            hour_of_day=hour_of_day,
            signals=OBJECTIVE_SIGNALS[objective],
            max_assets=max_assets,
            det_budget_s=det_budget_s,
            allow_rejection=True,
        )
        repeated = _conflicts(result["rows"], blocked)
        attempts.append({
            "attempt": attempt,
            "status": result["fire"]["status"],
            "rows": len(result["rows"]),
            "repeated_rejected_stalls": sorted(repeated),
        })
        if not repeated or attempt >= max_retries:
            result["fire"]["agent_objective"] = objective
            result["fire"]["feedback_count"] = len(feedback)
            result["fire"]["retry_attempts"] = attempts
            result["pipeline"] = {
                "primary": "cp_sat_forward_lex",
                "objective": objective,
                "attempts": len(attempts),
                "feedback_applied": bool(blocked),
            }
            return result
        working = _remove_stalls(working, repeated)

    raise AssertionError("bounded assignment loop did not return")
