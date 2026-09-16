from __future__ import annotations

import os

os.environ.setdefault("OTTOQ_CORE_ROOT", "../otto-q-core")

from app.optimizers.assignment_cpsat import optimize_assignment


RUN = "0a7d4569-888b-4633-a919-dbc4b8adca29"
DEPOT = "11111111-1111-1111-1111-111111111111"
V1 = "6e7d0b1c-0000-4000-8000-000000000001"
S1 = "5a11a000-0000-4000-8000-000000000001"
S2 = "5a11a000-0000-4000-8000-000000000002"

CLASS_ROWS = [{
    "vehicle_class_code": "waymo_jaguar_ipace_2024",
    "battery_capacity_kwh": "90",
    "max_charge_rate_kw": "100",
    "charge_kinds": ["dcfc", "l2"],
    "energy_curve": [{"above_soc_pct": 0, "accept_frac": 1.0}],
    "battery_chemistry": "NMC",
}]

SITE = {
    "power_cap_kw_hard": 2500,
    "power_soft_target_kw": 1620,
    "dcfc_cooldown_min": 18,
    "move_duration_min": 4,
    "path_capacity": 2,
    "cold_start_below_c": 5,
    "cold_start_penalty_min": 12,
    "onpeak_window_min": [0, 0],
}


def _frame():
    return {
        "vehicles": [{
            "id": V1,
            "state": "arrived_at_gate",
            "soc": 25,
            "stall_id": None,
            "inlet_type": "CCS1",
            "inlet_max_kw": 100,
            "target_soc": 90,
            "min_soc_threshold": 20,
            "vehicle_class_code": "waymo_jaguar_ipace_2024",
        }],
        "stalls": [
            {"id": S1, "type": "dcfc", "status": "available", "vehicle_id": None,
             "connector_type": "Multi", "connector_max_kw": 150,
             "supported_inlet_types": ["CCS1"]},
            {"id": S2, "type": "dcfc", "status": "available", "vehicle_id": None,
             "connector_type": "Multi", "connector_max_kw": 100,
             "supported_inlet_types": ["CCS1"]},
        ],
        "sessions": [],
        "energy": None,
        "bess": None,
    }


def test_cp_sat_is_primary_and_agent_objective_selects_the_regime():
    result = optimize_assignment(
        frame=_frame(), class_rows=CLASS_ROWS, site=SITE,
        sim_run_id=RUN, depot_id=DEPOT, objective="throughput_first",
        max_assets=4, det_budget_s=0.05,
    )
    assert result["pipeline"]["primary"] == "cp_sat_forward_lex"
    assert result["fire"]["solver"]["regime"] == "demand_surge"
    assert result["fire"]["solver"]["pass_modes"] == ["min_tardy", "min_flow"]
    assert result["rows"][0]["source"] == "forward_lex"


def test_rejection_feedback_forces_one_bounded_resolve():
    first = optimize_assignment(
        frame=_frame(), class_rows=CLASS_ROWS, site=SITE,
        sim_run_id=RUN, depot_id=DEPOT, objective="readiness_first",
        max_assets=4, det_budget_s=0.05,
    )
    rejected_stall = first["rows"][0]["proposal"]["stall_id"]
    result = optimize_assignment(
        frame=_frame(), class_rows=CLASS_ROWS, site=SITE,
        sim_run_id=RUN, depot_id=DEPOT, objective="readiness_first",
        feedback=[{"entity_id": V1, "stall_id": rejected_stall, "reason": "shield refused"}],
        max_assets=4, det_budget_s=0.05, max_retries=2,
    )
    assert result["pipeline"]["attempts"] == 2
    assert result["pipeline"]["feedback_applied"] is True
    assert result["rows"][0]["proposal"]["stall_id"] != rejected_stall
    assert result["fire"]["retry_attempts"][0]["repeated_rejected_stalls"] == [rejected_stall]
