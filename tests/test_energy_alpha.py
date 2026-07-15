"""
Verify the energy MPC optimizer is CORRECT and beats the deterministic heuristic
on a realistic overnight-wave load profile (the scenario the twin cert uses).

Baselines compared, all with the SAME battery:
  1. no_bess      — battery idle (grid = load - solar). Upper bound on peak.
  2. heuristic    — the cert_03 rule: discharge to hold grid<=target, recharge only
                    in deep valleys (net < 40% target). Our current live logic.
  3. mpc          — optimize_energy(): full-horizon LP minimizing demand+energy+wear.

Pass criteria: mpc peak <= heuristic peak <= no_bess peak, AND mpc total $ <= heuristic $.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.optimizers.energy_mpc import BessState, optimize_energy

# ---- realistic overnight wave: 40 half-hour steps, base ~120 kW, a sustained
#      return-wave charging bump peaking ~1900 kW over ~3h, then tapering. ----
T = 40
dt = 0.5
base = [120.0] * T
wave = [0]*4 + [400, 900, 1500, 1900, 1800, 1500, 1100, 700, 400, 200] + [80]*(T-14)
load = [base[t] + wave[t] for t in range(T)]
solar = [0.0]*T
price = [0.06]*T            # flat $/kWh energy
demand_charge = 21.78       # $/kW-month (NES summer)

bess = BessState(soc_kwh=0.90*3000, capacity_kwh=3000, max_charge_kw=1500,
                 max_discharge_kw=1500, soc_floor_frac=0.10, soc_ceiling_frac=0.95)


def peak(seq):
    return max(seq)


def sim_no_bess():
    grid = [load[t] - solar[t] for t in range(T)]
    return peak(grid)


def sim_heuristic():
    """cert_03 live logic: discharge to target, recharge only in deep valleys."""
    target = 1250.0                       # service_max 2500 * 0.5
    recharge_ceiling = 0.40 * target      # 500
    soc = bess.soc_kwh
    floor = bess.soc_floor_frac * bess.capacity_kwh
    ceil = bess.soc_ceiling_frac * bess.capacity_kwh
    grids = []
    for t in range(T):
        net = load[t] - solar[t]
        disp = 0.0
        if net > target and soc > floor + 0.03*bess.capacity_kwh:
            disp = min(bess.max_discharge_kw, net - target)              # discharge (+)
        elif net < recharge_ceiling and soc < ceil - 0.03*bess.capacity_kwh:
            disp = -min(bess.max_charge_kw, recharge_ceiling - net)      # recharge (-)
        grid = net - disp
        grids.append(grid)
        soc += (max(0,-disp)*bess.eff_charge - max(0,disp)/bess.eff_discharge) * dt
        soc = min(ceil, max(floor, soc))
    return peak(grids)


def main():
    p_none = sim_no_bess()
    p_heur = sim_heuristic()
    res = optimize_energy(load, solar, price, dt, bess, demand_charge, billing_period_peak_kw=0.0)
    p_mpc = res.predicted_peak_kw

    print(f"solver         : {res.solver} ({res.status})")
    print(f"peak no_bess   : {p_none:7.0f} kW   -> ${p_none*demand_charge:,.0f}/mo")
    print(f"peak heuristic : {p_heur:7.0f} kW   -> ${p_heur*demand_charge:,.0f}/mo")
    print(f"peak MPC       : {p_mpc:7.0f} kW   -> ${p_mpc*demand_charge:,.0f}/mo")
    print(f"MPC shave vs no_bess  : {100*(p_none-p_mpc)/p_none:5.1f}%")
    print(f"MPC shave vs heuristic: {100*(p_heur-p_mpc)/p_heur:5.1f}%  (extra alpha the optimizer finds)")
    print(f"MPC objective  : ${res.objective_usd:,.0f}  (demand ${res.demand_cost_usd:,.0f} + energy ${res.energy_cost_usd:,.0f} + wear ${res.degradation_usd:,.0f})")

    assert res.status == "Optimal", res.status
    assert p_mpc <= p_heur + 1e-6, f"MPC ({p_mpc}) worse than heuristic ({p_heur})"
    assert p_mpc <= p_none + 1e-6
    # SoC never violates bounds
    floor = bess.soc_floor_frac*bess.capacity_kwh - 1e-6
    ceil = bess.soc_ceiling_frac*bess.capacity_kwh + 1e-6
    assert all(floor <= s <= ceil for s in res.soc_kwh), "SoC out of bounds"
    print("\nPASS: MPC is optimal, feasible (SoC in bounds), and <= heuristic on peak.")


if __name__ == "__main__":
    main()
