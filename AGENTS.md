# AGENTS.md — ottoq-intelligence

**This repo is the frontier optimization and ML backbone for OTTO-Q** — a Python FastAPI service
hosting the optimizers and learned models that **cannot** live in Postgres, called over HTTP by the
twin and by Supabase edge functions.

## The doctrine, and it is not negotiable

> **model proposes → optimizer disposes → shield guarantees → loop learns**

**Every output of this service is ADVISORY.** OTTO-Q's deterministic 52-rule L1 shield has the final
word, and only the deterministic core enacts. Nothing here moves a vehicle.

Two founder rules this service must never violate:
- **Vehicles and chargers are never held back.** Energy shaping happens **only** via forecast + BESS
  + scheduling — never by denying a vehicle a plug.
- **A real ceiling must SHAPE the solution as an LP constraint, never post-hoc delete it.** This rule
  exists because an energy cap once discarded **100% of optimal solutions** *after* the solve — 21
  invocations returned `status:"Optimal"` and produced zero proposals.

## Where the layers stand

| Layer | Endpoint | Engine | Status |
|---|---|---|---|
| Optimize — Energy | `POST /optimize/energy` | rolling-horizon MILP/MPC (HiGHS) | **LIVE + tested** |
| Forecast | `POST /forecast` | probabilistic arrivals/load/SoC | stub |
| Optimize — Assign | `POST /assign` | NVIDIA cuOpt + job-shop | stub |
| Orchestrate | `POST /orchestrate` | Nemotron conductor | stub |
| Learn | offline | CIL / Bayesian tuning | future |

## Landmines specific to this repo

- 🔑 **Supabase's `pg_net` CANNOT reach a raw EC2 box** — TCP/SSL handshake timeout, because database
  egress is HTTPS/edge-function only. **The fix is an edge-function bridge** (`ottoq-energy-mpc`,
  `verify_jwt=false` + `x-bridge-token`, Deno `fetch` → AWS). **This is THE way the database calls any
  external compute** — the same path cuOpt and Nemotron use. Do not try to make the DB dial the box
  directly.
- **Latency is 30–60 s** (edge cold start + pg_net's batched worker), so the seam is deliberately
  **fire-and-ingest**: `ottoq_energy_mpc_replan(...)` fires and inserts a `pending` plan row;
  `ottoq_energy_mpc_ingest(plan_id)` completes it from the landed response. That matches out-of-band
  production replanning — do not "fix" it into a blocking call.
- ⚠️ **The bridge token and the AWS URL are hardcoded** in the edge function and in the replan
  defaults. **Move them to Supabase secrets before any real productionisation** — flag it, do not
  silently rewrite a deployed function.
- ⚠️ **The headline 38.3% shave is against the NAIVE heuristic.** A same-day adversarial refutation
  showed **a reserve-aware reactive controller matches the LP to the penny**
  (branch `fr1b-energy-optimizer-certs`). **Do not quote 38.3% against a straw baseline.** Establish
  the well-tuned reactive baseline and report against that.
- **This repo is public** — it was made public to resolve git-auth friction on the EC2 box.
  **Never commit a key, a token, a connection string, or customer data.**

## Verify before you PR

```bash
pip install -r requirements.txt
python tests/test_energy_alpha.py
```

Deployment notes live in `deploy/AWS_PLAYBOOK.md` and `deploy/DEPLOY_EC2.md`.
---

## The full context lives elsewhere

**Read this first, before any substantive work:**

```bash
git clone https://github.com/OTTOYARD/ottoyard-agent-context.git
```

That repository is the shared brain for agents on this project: architecture, the founder's binding
doctrine, the known-issues register, a ranked backlog, hard-won lessons, and a verbatim copy of the
79 memory files Claude Code accumulated while building this system. Start with its `README.md` and
`docs/16_FIRST_SESSION_RUNBOOK.md`.

## Rules that apply in every OTTOYARD repo

**⚖️ The law.** *OTTO-Q decides. OTTO-TWIN executes and owns world state. The renderer only draws.*
Decision-layer code that mutates world state is a defect on sight. Renderer code containing world
logic is a defect on sight.

**Branch, verify, PR. Never merge.** Chase Ballenger (founder) is the only one who merges. Branch as
`hermes/<slug>` or `claude/<slug>`, prove it works yourself, then open a PR whose description carries
**the evidence** — real numbers, real row counts, real screenshots — plus what you did *not* verify
and what could break. Half-done labelled half-done is fine; half-done labelled done is not.

**🚨 `git fetch origin` before you reason about anything.** The clones on the founder's Desktop have
been up to **77 commits behind**. Compare against `origin/main`, never local `main`. A branch still
existing is not evidence it is unmerged — check
`git rev-list --count origin/main..origin/<branch>` (0 means merged).

**Two identity traps.** `OTTOYARD` on GitHub is a **personal account, not an organization**
(`/orgs/OTTOYARD/...` returns 404 — use `/user/repos`). And GitHub **rejects pushes authored as
`chase@ottoyard.com`** — commit as a noreply identity.

**The one database that matters is `gxdrcyphqjzjsuhxuqtg`.** ⚠️ **Every `supabase/config.toml` in
every OTTOYARD repo points somewhere else** — at dead refs (`hfjaofyfxsyniohdfacg`,
`odhpbdhnpcrjeaxvbrzd`), at OrchestrAV's legacy database (`ycsisvozzgmisboumfqc`), or at a
placeholder. The real ref is hardcoded in client code instead. **Pass
`--project-ref gxdrcyphqjzjsuhxuqtg` explicitly to any Supabase CLI command that writes.**

**Never disable pg_cron job 12** (`ottoq-demo-metronome`). It **is** the simulation run engine.
Disabling it stops every run while everything still looks green.

**Honesty about numbers is a hard requirement here.** Always state your denominator. Never quote
`vehicles_turned_around`, `fleet_ready_pct`, or `gate_backlog` — they are final-frame instantaneous
counts that structurally penalise OTTO-Q. Interrogate the baseline before believing a win: it has
been invalid twice, both times in our favour. Read
`ottoyard-agent-context/memory/reference_ottoq_real_edge.md` before quoting any comparative figure.
