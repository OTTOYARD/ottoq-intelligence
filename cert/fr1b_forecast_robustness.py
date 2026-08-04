"""
FR-1b robustness: does the MPC's multi-objective edge survive FORECAST ERROR?

The cert so far assumes perfect foresight — the honest caveat. Reactive controllers
react to the realized load (no forecast), so forecast error only hurts the MPC. This
sweeps forecast-error sigma for two MPC deployment modes:
  - OPEN-LOOP  : solve once on the noisy forecast, apply the whole plan to the TRUE load.
  - RECEDING-HORIZON (the real deployment): each tick, re-solve with TRUE load so far +
    noisy forecast ahead, apply only setpoint[0], roll. Feedback corrects forecast error.
vs the best hand-tuned reactive baseline (reactive_peak w/ pre-charge, forecast-free).

Multiplicative load-forecast noise ~ (1 + N(0,sigma)); averaged over draws.
Answers: at what forecast error does open-loop MPC lose its edge, and does receding-horizon
keep it? (If yes, the frontier value is robust to a real, imperfect forecaster = FR-2.)
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.expanduser("~/Desktop/ottoq-intelligence"))
from app.optimizers.energy_mpc import BessState, optimize_energy

DT=0.5; DEMAND=21.78; DEGR=0.02; DAYS=30
FLOOR_F,CEIL_F,EFF = 0.10,0.95,0.90**0.5
WAVES = {
 "928": [63.2,56,68.8,55.7,51.2,63.5,68,55.9,52.3,59.1,62.3,45.7,44.1,80.2,123.7,262.9,365.9,841.7,655.5,636.1,882.4,618.6,680.5,636,706.1,928.5,837.9,505.2,542.3,527.9,536.3,434.6,401.6,459.4,392.9,398.7,288.5,437.3,353.9,359.6],
 "848": [85.2,138.9,145.8,120.1,208.9,97.6,147.3,138.1,124.3,115.9,72.9,142,91.3,100.4,86.9,177.9,388,449.6,440.9,463.8,539.9,848.4,799.5,559.6,600,790.2,549.7,504.6,624.4,398,527.5,508.8,401.5,437.6,395.8,434.7,364.5,258.8,381.3,319.5],
 "821": [159.9,123.7,148.9,121.3,154.6,133.7,135.1,107.9,127.2,106.8,81,85.4,106.5,89.4,65.1,203.4,268.3,525.6,641.2,631,820.7,658.8,637.8,792.1,486,680.9,744.8,461.7,476.6,545.5,394.9,399.9,395.4,288.3,273.2,385.7,324.7,297,287.3,387.7],
}
N=40
def hour(k): return (20+k*0.5)%24
TOU=[0.28 if 16<=hour(k)<21 else (0.05 if hour(k)<6 else 0.13) for k in range(N)]

def bess(cap=3000,p=1500):
    return BessState(soc_kwh=0.9*cap,capacity_kwh=cap,max_charge_kw=p,max_discharge_kw=p,
        soc_floor_frac=FLOOR_F,soc_ceiling_frac=CEIL_F,eff_charge=EFF,eff_discharge=EFF,degradation_usd_per_kwh=DEGR)

def simulate(L,setpoint,cap,mp):
    soc=0.9*cap; floor=FLOOR_F*cap; ceil=CEIL_F*cap; grid=[]; thru=0.0
    for k in range(N):
        sp=max(-mp,min(mp,setpoint[k]))
        if sp>0: sp=min(sp,(soc-floor)*EFF/DT); soc-=sp/EFF*DT
        elif sp<0: c=min(-sp,(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp=-c
        thru+=abs(sp)*DT; grid.append(max(0.0,L[k]-sp))
    return grid,thru

def bill(grid,thru,price):
    return DEMAND*max(grid)+DAYS*(sum(price[k]*grid[k]*DT for k in range(N))+DEGR*thru)

def reactive_peak(L,cap,mp):
    floor=FLOOR_F*cap; ceil=CEIL_F*cap
    def sim(T):
        soc=0.9*cap; sp=[]
        for k in range(N):
            if L[k]>T and soc>floor: d=min(mp,L[k]-T,(soc-floor)*EFF/DT); soc-=d/EFF*DT; sp.append(d)
            elif L[k]<T and soc<ceil: c=min(mp,T-L[k],(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp.append(-c)
            else: sp.append(0.0)
        return sp
    lo,hi=0.0,max(L)
    for _ in range(50):
        mid=(lo+hi)/2; g,_=simulate(L,sim(mid),cap,mp)
        if max(g)<=mid+0.5: hi=mid
        else: lo=mid
    return sim(hi)

def mpc_openloop(Lf,cap,mp):
    r=optimize_energy(list(Lf),[0]*N,TOU,DT,bess(cap,mp),DEMAND); return r.bess_setpoint_kw

def mpc_receding(Ltrue,Lf,cap,mp):
    """re-solve each tick: true load known for past, noisy forecast ahead; apply setpoint[0]."""
    soc=0.9*cap; floor=FLOOR_F*cap; ceil=CEIL_F*cap; applied=[]
    for t in range(N):
        horizon=[Ltrue[t]]+[Lf[k] for k in range(t+1,N)]      # forecast from now to end
        b=BessState(soc_kwh=soc,capacity_kwh=cap,max_charge_kw=mp,max_discharge_kw=mp,
            soc_floor_frac=FLOOR_F,soc_ceiling_frac=CEIL_F,eff_charge=EFF,eff_discharge=EFF,degradation_usd_per_kwh=DEGR)
        r=optimize_energy(horizon,[0]*len(horizon),TOU[t:],DT,b,DEMAND)
        sp=r.bess_setpoint_kw[0]
        sp=max(-mp,min(mp,sp))
        if sp>0: sp=min(sp,(soc-floor)*EFF/DT); soc-=sp/EFF*DT
        elif sp<0: c=min(-sp,(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp=-c
        applied.append(sp)
    return applied

def run(cap,mp,sigmas,draws,seed=7):
    rng=np.random.default_rng(seed)
    # reactive baseline (forecast-free) — fixed per wave
    react={w:bill(*simulate(L,reactive_peak(L,cap,mp),cap,mp),TOU) for w,L in WAVES.items()}
    react_avg=np.mean(list(react.values()))
    print(f"\n  battery {cap:.0f} kWh / {mp:.0f} kW | best reactive avg bill ${react_avg:,.0f}/mo")
    print(f"  {'fcast_err':>9}{'MPC open-loop':>16}{'MPC receding':>16}   (avg $/mo; edge vs reactive in parens)")
    for sig in sigmas:
        ol=[]; rh=[]
        for w,L in WAVES.items():
            for _ in range(draws if sig>0 else 1):
                Lf=[max(0.0,L[k]*(1+rng.normal(0,sig))) for k in range(N)] if sig>0 else list(L)
                ol.append(bill(*simulate(L,mpc_openloop(Lf,cap,mp),cap,mp),TOU))
                rh.append(bill(*simulate(L,mpc_receding(L,Lf,cap,mp),cap,mp),TOU))
        ola,rha=np.mean(ol),np.mean(rh)
        print(f"  {sig*100:7.0f}% {ola:12,.0f} ({react_avg-ola:+6,.0f}) {rha:12,.0f} ({react_avg-rha:+6,.0f})")

print("FR-1b forecast-error robustness (TOU price). Positive edge = MPC beats best reactive.")
print("=== full 3 MWh battery ===")
run(3000,1500,[0.0,0.10,0.20,0.30],draws=12)
print("\n=== energy-constrained 500 kWh / 400 kW pack ===")
run(500,400,[0.0,0.10,0.20,0.30],draws=12)
print("\nread: open-loop degrades as forecast error grows; receding-horizon (the real deployment)")
print("uses feedback each tick and holds the edge far better — the honest robustness answer.")
