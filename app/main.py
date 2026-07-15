"""
OTTO-Q Intelligence Service — the frontier optimization + ML backbone.

A hosted (AWS) FastAPI service the twin/edge-functions call for real optimization
and learned inference that cannot live in Postgres:

    POST /optimize/energy   rolling-horizon BESS+charge MPC (FR-1)         [LIVE]
    POST /forecast          probabilistic arrivals/load/SoC (FR-2)        [stub -> GPU model]
    POST /assign            cuOpt charger/bay/service assignment (FR-3)    [stub -> cuOpt]
    POST /orchestrate       Nemotron conductor over the stack (FR-4)      [stub -> NIM]
    GET  /health

Doctrine: model proposes, optimizer disposes, shield guarantees, loop learns.
Every optimizer output is advisory to the twin's deterministic safety shield.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.optimizers.energy_mpc import BessState, optimize_energy

app = FastAPI(title="OTTO-Q Intelligence Service", version="0.1.0")


class BessIn(BaseModel):
    soc_kwh: float
    capacity_kwh: float
    soc_floor_frac: float = 0.10
    soc_ceiling_frac: float = 0.95
    max_charge_kw: float = 1500.0
    max_discharge_kw: float = 1500.0
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    degradation_usd_per_kwh: float = 0.02


class EnergyOptIn(BaseModel):
    load_kw: List[float] = Field(..., description="forecast site load (base+EV) per step")
    solar_kw: List[float]
    energy_price_usd_per_kwh: List[float]
    tick_hours: float = 0.5
    bess: BessIn
    demand_charge_usd_per_kw: float = 21.78
    billing_period_peak_kw: float = 0.0
    end_soc_min_frac: Optional[float] = None
    allow_export: bool = False
    uncertainty_kw: Optional[List[float]] = None


@app.get("/health")
def health():
    return {"ok": True, "service": "ottoq-intelligence", "optimizers": ["energy_mpc"]}


@app.post("/optimize/energy")
def optimize_energy_endpoint(req: EnergyOptIn):
    res = optimize_energy(
        load_kw=req.load_kw,
        solar_kw=req.solar_kw,
        energy_price_usd_per_kwh=req.energy_price_usd_per_kwh,
        tick_hours=req.tick_hours,
        bess=BessState(**req.bess.model_dump()),
        demand_charge_usd_per_kw=req.demand_charge_usd_per_kw,
        billing_period_peak_kw=req.billing_period_peak_kw,
        end_soc_min_frac=req.end_soc_min_frac,
        allow_export=req.allow_export,
        uncertainty_kw=req.uncertainty_kw,
    )
    return res.__dict__


@app.post("/forecast")
def forecast_stub():
    # FR-2: replace with TFT/NHITS probabilistic forecaster served from the GPU tier.
    return {"status": "not_implemented", "note": "FR-2 learned forecaster pending"}


@app.post("/assign")
def assign_stub():
    # FR-3: replace with cuOpt (sync/batch, constraint-aware) charger/bay/service assignment.
    return {"status": "not_implemented", "note": "FR-3 cuOpt assignment pending"}


@app.post("/orchestrate")
def orchestrate_stub():
    # FR-4: Nemotron conductor (NIM) coordinating the specialist optimizers.
    return {"status": "not_implemented", "note": "FR-4 Nemotron conductor pending"}
