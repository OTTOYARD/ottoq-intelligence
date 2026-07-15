"""
FR-1b multi-objective properties of the energy optimizer (total bill, not just peak):
  1. Flat price, single objective -> MPC ties an optimal reactive clip (no free lunch claimed).
  2. TOU price -> MPC's total bill beats the best hand-tuned reactive controller (arbitrage
     on top of shaving) AND without inflating the peak.
  3. DR/feeder cap -> honored exactly when the battery can comply (0 violation); reported as
     unavoidable (soft) when it physically cannot — never by holding vehicles.
Noise-free counterfactual on a recorded real twin wave.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.optimizers.energy_mpc import BessState, optimize_energy

L = [63.2,56,68.8,55.7,51.2,63.5,68,55.9,52.3,59.1,62.3,45.7,44.1,80.2,123.7,262.9,365.9,841.7,
     655.5,636.1,882.4,618.6,680.5,636,706.1,928.5,837.9,505.2,542.3,527.9,536.3,434.6,401.6,459.4,
     392.9,398.7,288.5,437.3,353.9,359.6]
N = len(L); DT = 0.5; DEMAND = 21.78
def bess(cap=3000, p=1500): return BessState(soc_kwh=0.9*cap, capacity_kwh=cap, max_charge_kw=p,
    max_discharge_kw=p, soc_floor_frac=0.10, soc_ceiling_frac=0.95, eff_charge=0.9**0.5, eff_discharge=0.9**0.5)

def hour(k): return (20 + k*0.5) % 24
tou = [0.28 if 16<=hour(k)<21 else (0.05 if hour(k)<6 else 0.13) for k in range(N)]

def reactive_peak_grid(price):
    """optimal clip-to-target w/ pre-charge -> realized grid (best single-objective peak baseline)."""
    cap=3000.0; floor=0.10*cap; ceil=0.95*cap; eff=0.9**0.5; mp=1500.0
    def sim(T):
        soc=0.9*cap; g=[]
        for k in range(N):
            if L[k]>T and soc>floor: d=min(mp,L[k]-T,(soc-floor)*eff/DT); soc-=d/eff*DT; g.append(L[k]-d)
            elif L[k]<T and soc<ceil: c=min(mp,T-L[k],(ceil-soc)/(DT*eff)); soc+=c*eff*DT; g.append(L[k]+c)
            else: g.append(L[k])
        return [max(0,x) for x in g]
    lo,hi=0.0,max(L)
    for _ in range(50):
        mid=(lo+hi)/2; g=sim(mid)
        if max(g)<=mid+0.5: hi=mid
        else: lo=mid
    return sim(hi)

def energy_cost(grid, price): return sum(price[k]*grid[k]*DT for k in range(N))


def test_flat_price_ties_reactive():
    r = optimize_energy(L, [0]*N, [0.10]*N, DT, bess(), DEMAND)
    rg = reactive_peak_grid([0.10]*N)
    assert r.status == "Optimal"
    assert abs(r.predicted_peak_kw - max(rg)) < 1.0, "flat price: MPC should tie the reactive clip on peak"


def test_tou_never_worse_and_no_peak_inflation():
    # Invariant that always holds (magnitude of the win is scenario-dependent — see
    # scratchpad/fr1b_multiobjective_ab.py for the per-scenario economics): the co-optimizer
    # is never worse than the best single-objective reactive clip AND never inflates the peak
    # to chase energy savings. Here the reactive baseline is even given FREE battery wear.
    r = optimize_energy(L, [0]*N, tou, DT, bess(), DEMAND)
    rg = reactive_peak_grid(tou)
    mpc_bill = DEMAND*r.predicted_peak_kw + 30*(r.energy_cost_usd + r.degradation_usd)
    rct_bill = DEMAND*max(rg) + 30*energy_cost(rg, tou)   # reactive pays no wear -> stronger test
    assert r.status == "Optimal"
    assert r.predicted_peak_kw <= max(rg) + 1.0, "MPC must not inflate the peak while arbitraging"
    assert mpc_bill <= rct_bill + 1.0, f"MPC total bill must be <= a wear-free reactive ({mpc_bill:.0f} vs {rct_bill:.0f})"


def test_dr_cap_honored_when_feasible():
    cap = [600.0 if 14 <= k <= 27 else float("inf") for k in range(N)]
    r = optimize_energy(L, [0]*N, [0.06]*N, DT, bess(), DEMAND, grid_cap_kw=cap)
    assert r.status == "Optimal"
    assert r.dr_violation_kwh < 1.0, "feasible DR cap must be honored"
    assert all(g <= 601 for k, g in enumerate(r.grid_import_kw) if 14 <= k <= 27)


def test_dr_cap_soft_when_infeasible():
    cap = [400.0 if 14 <= k <= 27 else float("inf") for k in range(N)]
    r = optimize_energy(L, [0]*N, [0.06]*N, DT, bess(cap=300, p=300), DEMAND, grid_cap_kw=cap)
    assert r.status == "Optimal", "infeasible cap must still return a schedule (soft), not error"
    assert r.dr_violation_kwh > 1.0, "tiny battery cannot comply -> reports unavoidable violation"


if __name__ == "__main__":
    test_flat_price_ties_reactive()
    test_tou_never_worse_and_no_peak_inflation()
    test_dr_cap_honored_when_feasible()
    test_dr_cap_soft_when_infeasible()
    print("PASS: flat ties, TOU never worse + no peak inflation, DR cap honored/soft.")
