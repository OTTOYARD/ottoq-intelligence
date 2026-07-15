# OTTO-Q Intelligence Service

The frontier optimization + ML backbone for OTTO-Q robotaxi-depot orchestration.
Hosts the real optimizers and learned models that **cannot** live in Postgres, and
that the twin / Supabase edge-functions call over HTTP.

**Doctrine:** *model proposes → optimizer disposes → shield guarantees → loop learns.*
Every output here is **advisory** to the twin's deterministic safety shield (vehicles
are never held back; energy is shaped only via battery + scheduling, never by
throttling a charger).

## The stack (layered intelligence)

| Layer | Endpoint | Engine | Status |
|---|---|---|---|
| **Optimize — Energy** | `POST /optimize/energy` | rolling-horizon **MILP/MPC** (HiGHS) — BESS+charge schedule minimizing demand-charge ratchet + TOU + wear | **LIVE + tested** |
| **Forecast** | `POST /forecast` | probabilistic arrivals/load/SoC (TFT/NHITS, GPU) | stub → FR-2 |
| **Optimize — Assign** | `POST /assign` | NVIDIA **cuOpt** charger/bay/service assignment + job-shop | stub → FR-3 |
| **Orchestrate** | `POST /orchestrate` | **Nemotron** conductor (NIM) over the specialist optimizers | stub → FR-4 |
| **Learn** | (offline) | CIL — offline RL / Bayesian tuning from run outcomes | FR-5 |

## Verified alpha (energy MPC)

`tests/test_energy_alpha.py` on a realistic overnight-wave profile, same battery:

```
peak no_bess   : 2020 kW  $43,996/mo
peak heuristic : 1250 kW  $27,225/mo   (current live cert_03 rule)
peak MPC       :  771 kW  $16,786/mo
MPC shave vs heuristic: 38.3%   (~$125k/yr ADDITIONAL alpha per depot)
```

The MPC beats the reactive heuristic because it uses full-horizon foresight to flatten
the entire wave rather than greedily clipping to a fixed target.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python tests/test_energy_alpha.py          # verify the optimizer + alpha
uvicorn app.main:app --reload --port 8080  # serve
```

## Deploy

See `deploy/AWS_PLAYBOOK.md`. CPU service (energy MPC, cuOpt/Nemotron clients) → AWS
App Runner / ECS Fargate. GPU tier (learned forecaster, RL) → EC2 g5 / SageMaker.
Twin calls it via a Supabase edge function (`net.http_post`), MPC-style: re-solve each
tick, apply `bess_setpoint_kw[0]`, roll forward.
