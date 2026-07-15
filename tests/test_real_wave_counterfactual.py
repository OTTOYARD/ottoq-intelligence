"""
Real-data counterfactual: replay the ACTUAL per-tick load logged by a twin wave run
(benchmark depot, db000777 greedy, 40 half-hour ticks) through the energy MPC and
compare its peak to what the live heuristic actually billed (1093 kW).

This proves the optimizer's alpha on the twin's real logged conditions, not a
synthetic profile.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.optimizers.energy_mpc import BessState, optimize_energy

# building+lighting+EV charging per tick (kW), from site_energy_snapshots (real run)
load = [81.1, 92.5, 59.3, 60.1, 92.8, 87.2, 85.7, 107.2, 106.4, 116.7, 122.7, 117.2,
        69.9, 93.1, 358.9, 757.9, 630.5, 757.2, 917.0, 1092.9, 667.0, 1003.1, 280.5,
        129.6, 90.7, 160.0, 150.0, 230.9, 205.0, 418.8, 356.5, 461.9, 453.5, 408.5,
        337.5, 319.8, 519.6, 415.3, 332.3, 274.2]
T = len(load)
solar = [0.0] * T
price = [0.06] * T
dt = 0.5
demand_charge = 21.78
heuristic_actual_peak = 1093.0    # what the live cert_03 heuristic billed on this run

bess = BessState(soc_kwh=0.90 * 3000, capacity_kwh=3000, max_charge_kw=1500,
                 max_discharge_kw=1500, soc_floor_frac=0.10, soc_ceiling_frac=0.95)

res = optimize_energy(load, solar, price, dt, bess, demand_charge, billing_period_peak_kw=0.0)
p_mpc = res.predicted_peak_kw

print(f"solver               : {res.solver} ({res.status})")
print(f"raw load peak        : {max(load):7.0f} kW")
print(f"heuristic billed peak: {heuristic_actual_peak:7.0f} kW  -> ${heuristic_actual_peak*demand_charge:,.0f}/mo")
print(f"MPC peak             : {p_mpc:7.0f} kW  -> ${p_mpc*demand_charge:,.0f}/mo")
print(f"MPC vs heuristic     : {100*(heuristic_actual_peak-p_mpc)/heuristic_actual_peak:5.1f}% lower peak"
      f"  (~${(heuristic_actual_peak-p_mpc)*demand_charge*12:,.0f}/yr)")

assert res.status == "Optimal"
assert p_mpc < heuristic_actual_peak, "MPC did not beat the heuristic on real data"
floor = bess.soc_floor_frac*bess.capacity_kwh - 1e-6
ceil = bess.soc_ceiling_frac*bess.capacity_kwh + 1e-6
assert all(floor <= s <= ceil for s in res.soc_kwh), "SoC infeasible"
print("\nPASS: on the twin's REAL logged wave, the MPC beats the live heuristic and stays feasible.")
