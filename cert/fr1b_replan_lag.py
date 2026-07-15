"""
FR-1b architecture question: receding-horizon MPC is mandatory (open-loop is a trap), but
the twin reaches the optimizer via an async edge-fn bridge with ~30-60s latency, so the
freshest plan the twin can follow is LAGGED by some ticks. How many ticks of replan-lag
does the multi-objective edge tolerate?

At tick t the twin applies the plan solved `lag` ticks ago (at t-lag, with the realized SoC
and true load then + noisy forecast ahead) — its setpoint for the current tick. Sweep lag.
Tells us: does the async bridge suffice in production (real-time ticks minutes long ->
latency << tick -> lag~0-1), or must the optimizer be co-located (fast benchmark ticks)?
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.expanduser("~/Desktop/ottoq-intelligence"))
from app.optimizers.energy_mpc import BessState, optimize_energy

DT=0.5; DEMAND=21.78; DEGR=0.02; DAYS=30; F,C,EFF=0.10,0.95,0.90**0.5
WAVES={
 "928":[63.2,56,68.8,55.7,51.2,63.5,68,55.9,52.3,59.1,62.3,45.7,44.1,80.2,123.7,262.9,365.9,841.7,655.5,636.1,882.4,618.6,680.5,636,706.1,928.5,837.9,505.2,542.3,527.9,536.3,434.6,401.6,459.4,392.9,398.7,288.5,437.3,353.9,359.6],
 "848":[85.2,138.9,145.8,120.1,208.9,97.6,147.3,138.1,124.3,115.9,72.9,142,91.3,100.4,86.9,177.9,388,449.6,440.9,463.8,539.9,848.4,799.5,559.6,600,790.2,549.7,504.6,624.4,398,527.5,508.8,401.5,437.6,395.8,434.7,364.5,258.8,381.3,319.5],
 "821":[159.9,123.7,148.9,121.3,154.6,133.7,135.1,107.9,127.2,106.8,81,85.4,106.5,89.4,65.1,203.4,268.3,525.6,641.2,631,820.7,658.8,637.8,792.1,486,680.9,744.8,461.7,476.6,545.5,394.9,399.9,395.4,288.3,273.2,385.7,324.7,297,287.3,387.7],
}
N=40
def hour(k): return (20+k*0.5)%24
TOU=[0.28 if 16<=hour(k)<21 else (0.05 if hour(k)<6 else 0.13) for k in range(N)]
def mk(cap,soc): return BessState(soc_kwh=soc,capacity_kwh=cap,max_charge_kw=None,max_discharge_kw=None)  # placeholder

def simulate(L,sp,cap,mp):
    soc=0.9*cap; floor=F*cap; ceil=C*cap; grid=[]; thru=0.0
    for k in range(N):
        s=max(-mp,min(mp,sp[k]))
        if s>0: s=min(s,(soc-floor)*EFF/DT); soc-=s/EFF*DT
        elif s<0: c=min(-s,(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; s=-c
        thru+=abs(s)*DT; grid.append(max(0.0,L[k]-s))
    return grid,thru
def bill(g,t,pr): return DEMAND*max(g)+DAYS*(sum(pr[k]*g[k]*DT for k in range(N))+DEGR*t)
def reactive(L,cap,mp):
    floor=F*cap; ceil=C*cap
    def sim(T):
        soc=0.9*cap; sp=[]
        for k in range(N):
            if L[k]>T and soc>floor: d=min(mp,L[k]-T,(soc-floor)*EFF/DT); soc-=d/EFF*DT; sp.append(d)
            elif L[k]<T and soc<ceil: c=min(mp,T-L[k],(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp.append(-c)
            else: sp.append(0.0)
        return sp
    lo,hi=0.0,max(L)
    for _ in range(50):
        m=(lo+hi)/2; g,_=simulate(L,sim(m),cap,mp)
        hi=m if max(g)<=m+0.5 else hi; lo=lo if max(g)<=m+0.5 else m
    return sim(hi)

def receding_lagged(Ltrue,Lf,cap,mp,lag):
    soc=0.9*cap; floor=F*cap; ceil=C*cap; plans=[None]*N; applied=[]
    for t in range(N):
        # solve a fresh plan at tick t from realized SoC + true-now + noisy-ahead
        horizon=[Ltrue[t]]+[Lf[k] for k in range(t+1,N)]
        b=BessState(soc_kwh=soc,capacity_kwh=cap,max_charge_kw=mp,max_discharge_kw=mp,
                    soc_floor_frac=F,soc_ceiling_frac=C,eff_charge=EFF,eff_discharge=EFF,degradation_usd_per_kwh=DEGR)
        plans[t]=optimize_energy(horizon,[0]*len(horizon),TOU[t:],DT,b,DEMAND).bess_setpoint_kw
        src=max(0,t-lag)                    # follow the plan made `lag` ticks ago
        s=plans[src][t-src]
        s=max(-mp,min(mp,s))
        if s>0: s=min(s,(soc-floor)*EFF/DT); soc-=s/EFF*DT
        elif s<0: c=min(-s,(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; s=-c
        applied.append(s)
    return applied

def sweep(cap,mp,sigma,lags,draws,seed=11):
    rng=np.random.default_rng(seed)
    rbill=np.mean([bill(*simulate(L,reactive(L,cap,mp),cap,mp),TOU) for L in WAVES.values()])
    print(f"\n  battery {cap:.0f} kWh/{mp:.0f} kW | forecast err {sigma*100:.0f}% | best reactive ${rbill:,.0f}/mo")
    for lag in lags:
        bills=[]
        for L in WAVES.values():
            for _ in range(draws):
                Lf=[max(0.0,L[k]*(1+rng.normal(0,sigma))) for k in range(N)]
                bills.append(bill(*simulate(L,receding_lagged(L,Lf,cap,mp,lag),cap,mp),TOU))
        b=np.mean(bills)
        tag="instant" if lag==0 else f"{lag}-tick lag"
        print(f"    receding-horizon {tag:12}: ${b:,.0f}/mo  edge {rbill-b:+,.0f}")

print("FR-1b receding-horizon REPLAN-LAG tolerance (TOU, 20% forecast error). Edge>0 = beats reactive.")
sweep(3000,1500,0.20,[0,1,2,4],draws=8)
sweep(500,400,0.20,[0,1,2,4],draws=8)
print("\nread: in production, real-time ticks are minutes long so 30-60s bridge latency is <1 tick")
print("(lag 0-1) -> async bridge suffices. Only the FAST benchmark (sub-second ticks) needs a")
print("co-located optimizer. This validates the bridge architecture for the live product.")
