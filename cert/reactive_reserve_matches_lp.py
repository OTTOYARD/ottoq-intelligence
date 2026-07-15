"""
ADVERSARIAL Design 3 v4 — reserve-aware marginal-$/kWh reactive controller (NO LP).
Adds vs v3:
  * tight min-feasible-peak bisection using the ACTUAL physics oracle (kills demand rounding)
  * arbitrage split: (a) END-SURPLUS dump = battery energy never needed for any forced clip,
    dumped at the dearest ticks for pure gain (only wear); (b) REFILL-arbitrage only when a
    strictly cheaper refill exists.  -> fixes flat-price regression, shaves targets.
Every operation keeps grid <= T (peak) and SoC feasible. Causal / deployable.
"""
import sys, os
sys.path.insert(0, os.path.expanduser("~/Desktop/ottoq-intelligence"))
sys.path.insert(0, os.path.expanduser("~/Desktop/ottoq-intelligence/cert"))
from fr1b_multiobjective_ab import (WAVES, price_tou, price_flat, price_lmp, simulate, bill, mpc,
                                    N, DT, CAP_KWH, MAXP, FLOOR_F, CEIL_F, EFF, DEGR,
                                    reactive_peak, reactive_combined)
RT = EFF*EFF

def tight_peak(L, cap, maxp):
    """Min flat T such that clip-to-T with valley pre-charge yields realized peak<=T (physics oracle)."""
    floor=FLOOR_F*cap; ceil=CEIL_F*cap
    def realized_peak(T):
        soc=0.90*cap; sp=[]
        for k in range(N):
            if L[k]>T and soc>floor:
                d=min(maxp,L[k]-T,(soc-floor)*EFF/DT); soc-=d/EFF*DT; sp.append(d)
            elif L[k]<T and soc<ceil:
                c=min(maxp,T-L[k],(ceil-soc)/(DT*EFF)); soc+=c*EFF*DT; sp.append(-c)
            else: sp.append(0.0)
        g,_,_=simulate(L,sp,cap,maxp); return max(g)
    lo,hi=0.0,max(L)
    for _ in range(80):
        mid=(lo+hi)/2
        if realized_peak(mid)<=mid+1e-6: hi=mid
        else: lo=mid
    return hi

def backward_minsoc(L, T, cap, maxp):
    floor=FLOOR_F*cap; ceil=CEIL_F*cap
    req=[0.0]*(N+1); req[N]=floor
    for k in range(N-1,-1,-1):
        after=req[k+1]
        if L[k]>T:
            need=min(maxp, L[k]-T); req[k]=min(ceil, after + need/EFF*DT)
        else:
            cmax=min(maxp, max(0.0, T-L[k])); req[k]=max(floor, after - cmax*EFF*DT)
    return req

def reactive_reserve(L, price, cap, maxp, chg_pct=0.34, dis_pct=0.5, arb_hurdle=1.0):
    floor=FLOOR_F*cap; ceil=CEIL_F*cap; soc0=0.90*cap
    T=tight_peak(L, cap, maxp)
    minSoC=backward_minsoc(L, T, cap, maxp)
    ps=sorted(price); chg_thr=ps[int(chg_pct*(N-1))]; dis_thr=ps[int(dis_pct*(N-1))]
    cheapest_future=[0.0]*(N+1); cheapest_future[N]=float('inf')
    for k in range(N-1,-1,-1): cheapest_future[k]=min(price[k], cheapest_future[k+1])
    # forced battery-energy still ahead of each tick (no-refill need)
    forced_ahead=[0.0]*(N+1)
    for k in range(N-1,-1,-1):
        need=min(maxp, max(0.0,L[k]-T))/EFF*DT if L[k]>T else 0.0
        forced_ahead[k]=forced_ahead[k+1]+need
    # PLAN end-surplus dumps at the dearest eligible ticks
    end_surplus=max(0.0, soc0 - floor - forced_ahead[0])   # battery kWh never needed
    dump=[0.0]*N
    rem=end_surplus
    for k in sorted(range(N), key=lambda k:-price[k]):
        if rem<=1e-9: break
        if L[k]>T: continue                    # forced tick (handled separately)
        if price[k] < dis_thr: continue        # only worth dumping at dear-ish ticks
        d=min(maxp, L[k], rem*EFF/DT)
        if d<=1e-9: continue
        dump[k]=d; rem-=d/EFF*DT
    sp=[0.0]*N; soc=soc0
    for k in range(N):
        if L[k]>T:                                             # forced clip
            d=min(maxp, L[k]-T, (soc-floor)*EFF/DT); sp[k]=d; soc-=d/EFF*DT; continue
        headroom=T-L[k]
        # mandatory reserve top-up for upcoming forced clips
        if minSoC[k+1] > soc + 1e-9 and headroom>0 and soc<ceil:
            c=min(maxp, headroom, (ceil-soc)/(DT*EFF), (minSoC[k+1]-soc)/(EFF*DT)); sp[k]=-c; soc+=c*EFF*DT; continue
        safe=max(0.0,(soc - max(floor,minSoC[k+1]))*EFF/DT)    # dischargeable w/o breaking forced
        # (a) end-surplus dump (pure gain, never refilled)
        want=min(dump[k], safe, L[k])
        # (b) refill-arbitrage discharge: dear now, cheaper refill exists later
        if price[k]>=dis_thr and price[k] > cheapest_future[k+1]/RT + arb_hurdle*DEGR:
            want=min(max(want, min(maxp,L[k],safe)), maxp, L[k], safe)
        if want>1e-9:
            sp[k]=want; soc-=want/EFF*DT; continue
        # (c) cheap charge to fund a strictly-dearer future discharge
        if price[k]<=chg_thr and headroom>0 and soc<ceil:
            future_dear=any((L[j]>T or price[j]>=dis_thr) and price[j] > price[k]/RT + arb_hurdle*DEGR
                            for j in range(k+1,N))
            if future_dear:
                c=min(maxp, headroom, (ceil-soc)/(DT*EFF)); sp[k]=-c; soc+=c*EFF*DT
    return sp

def eval_ctrl(fn, price, cap, maxp, **kw):
    agg={"monthly":0.0,"peak":0.0,"energy_shift":0.0,"demand":0.0,"degr_shift":0.0}
    for L in WAVES.values():
        sp=fn(L,price,cap,maxp,**kw) if kw else fn(L,price,cap,maxp)
        g,thru,_=simulate(L,sp,cap,maxp); bl=bill(g,thru,price)
        for k in agg: agg[k]+=bl[k]/len(WAVES)
    return agg
def mpc_eval(price, cap, maxp):
    agg={"monthly":0.0,"peak":0.0,"energy_shift":0.0,"demand":0.0,"degr_shift":0.0}
    for L in WAVES.values():
        sp,_=mpc(L,price,cap,maxp); g,thru,_=simulate(L,sp,cap,maxp); bl=bill(g,thru,price)
        for k in agg: agg[k]+=bl[k]/len(WAVES)
    return agg
def report(tag, price, cap, maxp, **kw):
    m=mpc_eval(price,cap,maxp); mine=eval_ctrl(reactive_reserve,price,cap,maxp,**kw)
    rp=eval_ctrl(reactive_peak,price,cap,maxp); rc=eval_ctrl(reactive_combined,price,cap,maxp)
    print(f"\n=== {tag} cap={cap} maxp={maxp} kw={kw} ===")
    print(f"{'ctrl':18}{'peak':>7}{'demand$':>9}{'energy$':>9}{'degr$':>7}{'monthly$':>10}")
    for name,v in [("MPC",m),("reactive_peak",rp),("reactive_combined",rc),("reactive_reserve",mine)]:
        print(f"{name:18}{v['peak']:7.1f}{v['demand']:9.0f}{v['energy_shift']:9.0f}{v['degr_shift']:7.0f}{v['monthly']:10.1f}")
    gap=mine['monthly']-m['monthly']; pct=100*gap/m['monthly']
    print(f"  --> gap vs MPC: ${gap:,.1f}/mo (${gap*12:,.0f}/yr)  {pct:+.3f}%  {'BEAT/TIE' if pct<=0.5 else 'MPC wins'}")
    return gap
if __name__=="__main__":
    report("S2 TOU", price_tou(), CAP_KWH, MAXP)
    report("S5 constrained TOU", price_tou(), 500, 400)
    report("S1 FLAT", price_flat(), CAP_KWH, MAXP)
    report("S3 LMP", price_lmp(), CAP_KWH, MAXP)
