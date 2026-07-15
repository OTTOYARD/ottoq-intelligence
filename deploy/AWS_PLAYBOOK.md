# AWS deploy playbook — OTTO-Q Intelligence Service

Chase has ~$100k AWS credits (NVIDIA Inception). Two tiers.

## Tier 1 — CPU service (energy MPC, cuOpt/Nemotron clients) — ship first

Fastest: **AWS App Runner** from the Dockerfile (no cluster to manage).

```bash
# 1. build + push to ECR
aws ecr create-repository --repository-name ottoq-intelligence
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-1.amazonaws.com
docker build -t ottoq-intelligence . && docker tag ottoq-intelligence:latest <acct>.dkr.ecr.us-east-1.amazonaws.com/ottoq-intelligence:latest
docker push <acct>.dkr.ecr.us-east-1.amazonaws.com/ottoq-intelligence:latest

# 2. App Runner service (2 vCPU / 4 GB is plenty for the LP)
aws apprunner create-service --service-name ottoq-intelligence \
  --source-configuration '{"ImageRepository":{"ImageIdentifier":"<acct>.dkr.ecr.us-east-1.amazonaws.com/ottoq-intelligence:latest","ImageConfiguration":{"Port":"8080"},"ImageRepositoryType":"ECR"}}'
```
Result: an HTTPS URL. Put it + a shared bearer token in Supabase secrets; the twin's
edge function calls `POST {URL}/optimize/energy`.

Alt: ECS Fargate (Terraform in `deploy/terraform/` — TODO) if you want VPC/autoscaling.

## Tier 2 — GPU tier (FR-2 learned forecaster, FR-5 RL) — when we get there

- **EC2 `g5.xlarge`** (1× A10G) for serving TFT/NHITS via Triton or a small FastAPI+torch
  image, or **SageMaker** real-time endpoint. Add `torch`, `neuralforecast` to a
  `Dockerfile.gpu` (base `nvcr.io/nvidia/pytorch`).
- **NVIDIA hosted APIs** (cuOpt, Nemotron/NIM) need **no GPU** — call `optimize.api.nvidia.com`
  / `integrate.api.nvidia.com` with the Inception key. Use these first for FR-3/FR-4.
- **Physical AI** (Cosmos world models, Isaac Sim/Lab) → the parked GPU spend; separate track.

## Security
- Service behind a bearer token (Supabase secret). No PII leaves the twin — only aggregate
  load/SoC/price arrays. Never send credentials in the payload.
