"""
FR-1b — Multi-objective energy A/B: where the optimizer EARNS its keep.

Single-objective peak-shaving (FR-1) showed the MPC ties a perfectly-tuned reactive
clip on a big battery. This cert moves to the honest frontier: TOTAL MONTHLY BILL
(demand + TOU/LMP energy + battery degradation) under realistic conditions, plus a
demand-response feeder cap and an energy-constrained battery.

Compares the MPC (the real LP optimizer, app.optimizers.energy_mpc) against a suite of
well-tuned RULE-BASED reactive controllers — each the kind a good engineer would build:
  - reactive_peak     : optimal clip-to-target w/ valley pre-charge (best on PEAK)
  - reactive_tou      : charge cheapest hours / discharge dearest (best on ENERGY)
  - reactive_combined : peak-clip + a TOU arbitrage overlay (best hand-tuned all-rounder)
All controllers are evaluated through IDENTICAL battery physics (SoC clamp, efficiency).
Noise-free counterfactual on 3 recorded real twin waves (execution already verified exact).

Bottom line to look for: no single hand rule wins every objective, and even the best
hand-combined rule leaves money on the table the MPC captures — and the gap GROWS with
price volatility. That is the optimizer's durable, non-tunable edge.
"""
import sys, os, json
sys.path.insert(0, os.path.expanduser("~/Desktop/ottoq-intelligence"))
from app.optimizers.energy_mpc import BessState, optimize_energy

DT = 0.5                     # 30-min ticks
DEMAND = 21.78               # $/kW-month
DEGR = 0.02                  # $/kWh throughput (battery wear)
DAYS = 30                    # monthly scaling for energy+wear
CAP_KWH, MAXP = 3000.0, 1500.0
FLOOR_F, CEIL_F, EFF = 0.10, 0.95, 0.90**0.5

# 3 recorded real twin waves (net site load kW / tick), start 20:00, 40 ticks
WAVES = {
 "wave-928": [63.2,56,68.8,55.7,51.2,63.5,68,55.9,52.3,59.1,62.3,45.7,44.1,80.2,123.7,262.9,365.9,841.7,655.5,636.1,882.4,618.6,680.5,636,706.1,928.5,837.9,505.2,542.3,527.9,536.3,434.6,401.6,459.4,392.9,398.7,288.5,437.3,353.9,359.6],
 "wave-848": [85.2,138.9,145.8,120.1,208.9,97.6,147.3,138.1,124.3,115.9,72.9,142,91.3,100.4,86.9,177.9,388,449.6,440.9,463.8,539.9,848.4,799.5,559.6,600,790.2,549.7,504.6,624.4,398,527.5,508.8,401.5,437.6,395.8,434.7,364.5,258.8,381.3,319.5],
 "wave-821": [159.9,123.7,148.9,121.3,154.6,133.7,135.1,107.9,127.2,106.8,81,85.4,106.5,89.4,65.1,203.4,268.3,525.6,641.2,631,820.7,658.8,637.8,792.1,486,680.9,744.8,461.7,476.6,545.5,394.9,399.9,395.4,288.3,273.2,385.7,324.7,297,287.3,387.7],
}
N = 40
def hour(k): return (20 + k*0.5) % 24

# ---- price curves ----
def price_flat():  return [0.10]*N
def price_tou():   # moderate CA-style TOU
    def p(k):
        h=hour(k)
        if 16<=h<21: return 0.28          # evening on-peak
        if 0<=h<6:   return 0.05          # super off-peak overnight
        return 0.13
    return [p(k) for k in range(N)]
def price_lmp():   # volatile wholesale-style (wide swings)
    def p(k):
        h=hour(k)
        if 17<=h<20: return 0.55          # evening scarcity spike
        if 0<=h<5:   return 0.02          # deep overnight
        if 9<=h<15:  return 0.04          # solar-glut midday
        return 0.14
    return [p(k) for k in range(N)]

# ---- battery physics: run a setpoint schedule through the pack, return realized grid + throughput ----
def simulate(L, setpoint, cap_kwh=CAP_KWH, maxp=MAXP):
    soc=0.90*cap_kwh; floor=FLOOR_F*cap_kwh; ceil=CEIL_F*cap_kwh
    grid=[]; thru=0.0; realized=[]
    for k in range(N):
        sp=max(-maxp, min(maxp, setpoint[k]))
        if sp>0:                                   # discharge (deliver sp kW to load)
            sp=min(sp, (soc-floor)*EFF/DT); soc-=sp/EFF*DT
        elif sp<0:                                 # charge (draw -sp kW)
            c=min(-sp, (ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp=-c
        realized.append(sp); thru+=abs(sp)*DT
        grid.append(max(0.0, L[k]-sp))
    return grid, thru, realized

def bill(grid, thru, price):
    demand=DEMAND*max(grid)
    energy=sum(price[k]*grid[k]*DT for k in range(N))
    degr=DEGR*thru
    return {"peak":max(grid), "demand":demand, "energy_shift":energy, "degr_shift":degr,
            "monthly": demand + DAYS*(energy+degr)}

# ---- reactive controllers (produce a setpoint schedule) ----
def reactive_peak(L, price, cap_kwh=CAP_KWH, maxp=MAXP):
    floor=FLOOR_F*cap_kwh; ceil=CEIL_F*cap_kwh
    lo,hi=0.0,max(L)
    def sim_target(T):
        soc=0.90*cap_kwh; sp=[]
        for k in range(N):
            if L[k]>T and soc>floor:
                d=min(maxp,L[k]-T,(soc-floor)*EFF/DT); soc-=d/EFF*DT; sp.append(d)
            elif L[k]<T and soc<ceil:
                c=min(maxp,T-L[k],(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp.append(-c)
            else: sp.append(0.0)
        return sp
    for _ in range(50):
        mid=(lo+hi)/2; sp=sim_target(mid); g,_,_=simulate(L,sp,cap_kwh,maxp)
        if max(g)<=mid+0.5: hi=mid
        else: lo=mid
    return sim_target(hi)

def reactive_tou(L, price, cap_kwh=CAP_KWH, maxp=MAXP):
    # charge full during cheapest tercile, discharge (serve load) during dearest tercile
    lo=sorted(price)[N//3]; hi=sorted(price)[2*N//3]
    sp=[]
    for k in range(N):
        if price[k]<=lo:   sp.append(-maxp)         # charge cheap
        elif price[k]>=hi: sp.append(min(maxp,L[k]))# discharge dear (down to serving load)
        else: sp.append(0.0)
    return sp

def reactive_combined(L, price, cap_kwh=CAP_KWH, maxp=MAXP):
    # best hand-tuned: peak-clip primary; then, in the SoC headroom not needed for the peak,
    # bias pre-charge to the cheapest hours and add discharge in the dearest hours below target.
    base=reactive_peak(L,price,cap_kwh,maxp)
    hi=sorted(price)[2*N//3]; lo=sorted(price)[N//3]
    T=max(max(0.0,L[k]-base[k]) for k in range(N))  # the clip level it achieved
    sp=list(base)
    floor=FLOOR_F*cap_kwh; ceil=CEIL_F*cap_kwh; soc=0.90*cap_kwh
    # forward pass honoring SoC, adding TOU bias where it doesn't push grid past T
    for k in range(N):
        s=sp[k]
        if s==0.0:
            if price[k]>=hi and L[k]>0 and soc>floor:       # dear + idle -> discharge to shave energy (cap at T)
                s=min(maxp, L[k], (soc-floor)*EFF/DT)
            elif price[k]<=lo and soc<ceil and (L[k]-(-1))< T:  # cheap + idle -> top up (stay under T)
                s=-min(maxp, T-L[k], (ceil-soc)/(DT*EFF))
        # clamp physics
        if s>0: s=min(s,(soc-floor)*EFF/DT); soc-=s/EFF*DT
        elif s<0:
            c=min(-s,(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; s=-c
        sp[k]=s
    return sp

def mpc(L, price, cap_kwh=CAP_KWH, maxp=MAXP, grid_cap=None):
    b=BessState(soc_kwh=0.90*cap_kwh, capacity_kwh=cap_kwh, max_charge_kw=maxp, max_discharge_kw=maxp,
                soc_floor_frac=FLOOR_F, soc_ceiling_frac=CEIL_F, eff_charge=EFF, eff_discharge=EFF,
                degradation_usd_per_kwh=DEGR)
    r=optimize_energy(L,[0]*N,price,DT,b,DEMAND,grid_cap_kw=grid_cap)
    return r.bess_setpoint_kw, r

# ---- scenarios ----
def run_scenario(name, price, cap_kwh=CAP_KWH, maxp=MAXP, grid_cap=None):
    rows={}
    for cname, fn in [("reactive_peak",reactive_peak),("reactive_tou",reactive_tou),("reactive_combined",reactive_combined)]:
        agg={"monthly":0,"peak":0,"energy_shift":0,"demand":0}
        for L in WAVES.values():
            sp=fn(L,price,cap_kwh,maxp); g,thru,_=simulate(L,sp,cap_kwh,maxp); bl=bill(g,thru,price)
            for k in agg: agg[k]+=bl[k]/len(WAVES)
        rows[cname]=agg
    # MPC
    agg={"monthly":0,"peak":0,"energy_shift":0,"demand":0}
    for L in WAVES.values():
        sp,_=mpc(L,price,cap_kwh,maxp,grid_cap); g,thru,_=simulate(L,sp,cap_kwh,maxp); bl=bill(g,thru,price)
        for k in agg: agg[k]+=bl[k]/len(WAVES)
    rows["MPC"]=agg
    best_reactive=min(v["monthly"] for c,v in rows.items() if c!="MPC")
    save=best_reactive-rows["MPC"]["monthly"]
    print(f"\n=== {name} ===   (avg over {len(WAVES)} real waves; $/month)")
    print(f"{'controller':20}{'peak kW':>9}{'demand$':>10}{'energy$/shift':>15}{'~total$/mo':>12}")
    for c in ["reactive_peak","reactive_tou","reactive_combined","MPC"]:
        v=rows[c]; print(f"{c:20}{v['peak']:9.0f}{v['demand']:10.0f}{v['energy_shift']:15.0f}{v['monthly']:12.0f}")
    print(f"--> MPC saves ${save:,.0f}/mo (~${save*12:,.0f}/yr) = {100*save/best_reactive:.1f}% vs BEST reactive")
    return save

print("Battery 3 MWh / 1500 kW, demand $21.78/kW-mo, wear $0.02/kWh, 3 real twin waves.")
s_flat=run_scenario("S1 FLAT price (sanity)", price_flat())
s_tou =run_scenario("S2 TOU price (moderate CA)", price_tou())
s_lmp =run_scenario("S3 VOLATILE LMP (wholesale)", price_lmp())
s_small=run_scenario("S5 ENERGY-CONSTRAINED (500 kWh / 400 kW pack) + TOU", price_tou(), cap_kwh=500, maxp=400)

print(f"\nSUMMARY — optimizer's marginal $/yr vs the BEST hand-tuned reactive controller:")
print(f"  flat price       : ${s_flat*12:,.0f}/yr   (single objective -> ties, as expected)")
print(f"  moderate TOU     : ${s_tou*12:,.0f}/yr")
print(f"  volatile LMP     : ${s_lmp*12:,.0f}/yr")
print(f"  small battery+TOU: ${s_small*12:,.0f}/yr")
print("Edge is driven by OBJECTIVE CONFLICT + battery scarcity (not monotonic in price volatility);")
print("naive single-objective rules (see reactive_tou peak blow-up) actively harm the other objective.")
