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

import os
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.optimizers.energy_mpc import BessState, optimize_energy
from app.forecasters.priors import load_priors
from app.forecasters.statistical import forecast

app = FastAPI(title="OTTO-Q Intelligence Service", version="0.1.0")

# Shared-secret bearer auth. Set OTTOQ_API_TOKEN in the container env; the twin
# sends it as `Authorization: Bearer <token>`.
#
# THIS USED TO FAIL OPEN, AND THE DEPLOY MAKES THAT SERIOUS. The guard read
# `if _API_TOKEN and authorization != ...`, so an unset OTTOQ_API_TOKEN
# short-circuited the comparison and every request was allowed. deploy/
# DEPLOY_EC2.md instructs opening TCP 8080 to 0.0.0.0/0 with the sentence
# "it's protected by a bearer token below" -- which was true only while the
# variable happened to be set. One missing env var and the sentence became
# false silently, with nothing in the response to say so.
#
# It now fails CLOSED: no token configured means no request is served. Local
# development opts out explicitly with OTTOQ_ALLOW_UNAUTHENTICATED=1, which is
# a thing you have to type and can grep the fleet for -- unlike the absence of
# a variable, which looks identical to a correct deployment.
_API_TOKEN = os.environ.get("OTTOQ_API_TOKEN")
_ALLOW_UNAUTHENTICATED = os.environ.get("OTTOQ_ALLOW_UNAUTHENTICATED") == "1"


def require_token(authorization: str = Header(default="")):
    if _ALLOW_UNAUTHENTICATED:
        return
    if not _API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="service not configured: set OTTOQ_API_TOKEN, or "
                   "OTTOQ_ALLOW_UNAUTHENTICATED=1 for local development",
        )
    if authorization != f"Bearer {_API_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


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
    grid_cap_kw: Optional[List[float]] = None       # demand-response / feeder ceiling per step
    dr_penalty_usd_per_kwh: float = 5.0


@app.get("/health")
def health():
    return {"ok": True, "service": "ottoq-intelligence", "optimizers": ["energy_mpc"]}


@app.post("/optimize/energy", dependencies=[Depends(require_token)])
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
        grid_cap_kw=req.grid_cap_kw,
        dr_penalty_usd_per_kwh=req.dr_penalty_usd_per_kwh,
    )
    return res.__dict__


class ForecastIn(BaseModel):
    fleet_size: int = Field(..., description="number of vehicles in the depot fleet")
    turns_per_day: float = Field(..., description="expected returns per vehicle per day")
    base_load_kw: float = Field(..., description="site non-EV baseline load at mean")
    ev_daily_sessions: float = Field(..., description="expected EV charging sessions per day")
    departure_soc_pct: float = Field(..., description="typical SoC at departure (%)")
    target_soc_pct: float = Field(..., description="target SoC to restore vehicles to (%)")
    battery_kwh: float = Field(..., description="representative battery capacity (kWh)")
    horizon_hours: int = 24
    start_hour: int = Field(0, ge=0, le=23, description="hour of day the horizon begins")
    dow: int = Field(0, ge=0, le=6, description="0=Monday .. 6=Sunday")


@app.post("/forecast", dependencies=[Depends(require_token)])
def forecast_endpoint(req: ForecastIn):
    # FR-2: statistical, demand-side forecast. Built on the real-world
    # calibration priors (ACN-Data, NYC TLC, EIA, NREL), never on sim output.
    # The neural (TFT/NHITS) forecaster is a later GPU-tier upgrade behind the
    # same interface — see requirements.txt and app/forecasters/statistical.py.
    priors = load_priors()
    return forecast(
        priors,
        fleet_size=req.fleet_size,
        turns_per_day=req.turns_per_day,
        base_load_kw=req.base_load_kw,
        ev_daily_sessions=req.ev_daily_sessions,
        departure_soc_pct=req.departure_soc_pct,
        target_soc_pct=req.target_soc_pct,
        battery_kwh=req.battery_kwh,
        horizon_hours=req.horizon_hours,
        start_hour=req.start_hour,
        dow=req.dow,
    )


@app.post("/assign")
def assign_stub():
    # FR-3: replace with cuOpt (sync/batch, constraint-aware) charger/bay/service assignment.
    return {"status": "not_implemented", "note": "FR-3 cuOpt assignment pending"}


@app.post("/orchestrate")
def orchestrate_stub():
    # FR-4: Nemotron conductor (NIM) coordinating the specialist optimizers.
    return {"status": "not_implemented", "note": "FR-4 Nemotron conductor pending"}
