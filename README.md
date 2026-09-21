# OTTO-Q Intelligence Service

The optimization + ML sidecar for OTTO-Q robotaxi-depot orchestration: it hosts
optimizers and models that cannot live in Postgres, called by the twin / Supabase
edge-functions over HTTP. Honest status: **three of the five layers are implemented**
(energy MPC, assignment, forecast) and two are stubs (orchestrate, learn) — see the layer
table, corrected 2026-09-20 after two rows were found describing the unbuilt future rather
than the shipped present. Production currently runs with `energy_mpc_follow=0`, so this
service is advisory input, not the live backbone.

**Doctrine:** *model proposes → optimizer disposes → shield guarantees → loop learns.*
Every output here is **advisory** to the twin's deterministic safety shield (vehicles
are never held back; energy is shaped only via battery + scheduling, never by
throttling a charger).

## The stack (layered intelligence)

| Layer | Endpoint | Engine | Status |
|---|---|---|---|
| **Optimize — Energy** | `POST /optimize/energy` | rolling-horizon **MILP/MPC** (HiGHS) — BESS+charge schedule minimizing demand-charge ratchet + TOU + wear | **LIVE + tested** |
| **Forecast** | `POST /forecast` | statistical arrivals/load/SoC from the real-world calibration priors (ACN-Data, NYC TLC, EIA, NREL) — `forecasters/priors.py` + `forecasters/statistical.py`, never fitted on sim output | **LIVE** (the TFT/NHITS GPU forecaster is a later upgrade behind the same interface) |
| **Optimize — Assign** | `POST /assign` | **CP-SAT**, not cuOpt — `optimizers/assignment_cpsat.py` calls `bridge.proposer_bridge.fire` from the pinned otto-q-core checkout (`OTTOQ_CORE_REF`). The kernel still disposes. | **LIVE** (dark in-engine only while `OTTOQ_INTEL_URL`/`OTTOQ_INTEL_TOKEN` are unset) |
| **Orchestrate** | `POST /orchestrate` | **Nemotron** conductor (NIM) over the specialist optimizers | stub → FR-4 |
| **Learn** | (offline) | CIL — offline RL / Bayesian tuning from run outcomes | FR-5 |

### Why two rows were wrong, and which way

Both understated the service, and one named the wrong solver — which matters more than a
stale label. `/assign` was described as *"NVIDIA cuOpt charger/bay/service assignment"*,
and it has never been cuOpt: it calls CP-SAT through otto-q-core's proposer bridge.
otto-q-core's CLAUDE.md §2.5 rests on a **forced decomposition** — CP-SAT schedules
*inside* a site, cuOpt routes recalls *between* sites, each getting the problem shape it
is built for — so a reader of this table would have taken the sidecar's site-level
assignment layer for the one thing the vendor documentation says cuOpt cannot express.

`/forecast` was labelled a stub while describing the *neural* forecaster that is still
future work. The statistical one behind the same interface is implemented and is built on
the calibration priors rather than on sim output, which is the property that makes it
worth anything.

`/orchestrate` and `Learn` are genuinely stubs; `orchestrate` returns
`{"status": "not_implemented"}` and says so.

## Verified alpha (energy MPC)

`tests/test_energy_alpha.py` on a realistic overnight-wave profile, same battery:

```
peak no_bess   : 2020 kW  $43,996/mo
peak heuristic : 1250 kW  $27,225/mo   (current live cert_03 rule)
peak MPC       :  771 kW  $16,786/mo
MPC shave vs naive heuristic: 38.3%
```

**Do not quote the 38.3% (or a $/yr figure derived from it) externally.** Per AGENTS.md:
that comparison is against the naive fixed-target heuristic, and an adversarial
refutation showed a well-tuned reserve-aware reactive controller matches the LP to the
penny (branch `fr1b-energy-optimizer-certs`, `cert/reactive_reserve_matches_lp.py`).
The honest claim is: full-horizon MPC and a well-tuned reactive controller both flatten
the wave far below the naive rule; MPC's remaining edge must be established against that
tuned baseline before any number ships.

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
