# Deploy the OTTO-Q Intelligence Service — EC2 (App Runner is closed to new customers)

App Runner stopped taking new customers (Apr 30 2026). We use a plain **EC2 instance**
instead — it's the box we grow into the GPU tier (FR-2) anyway, and it's ~10 min.
(Managed alternative if you prefer no server: **Amazon ECS Express Mode**, AWS's
recommended App Runner replacement — ask me and I'll give those steps instead.)

## 1. Launch the instance (AWS Console → EC2 → Launch instance)
- **Name:** ottoq-intelligence
- **AMI:** Ubuntu Server 24.04 LTS
- **Instance type:** `t3.medium` (2 vCPU / 4 GB — plenty for the LP)
- **Key pair:** create/choose one (you'll SSH with it)
- **Network / Security group → allow:**
  - SSH (22) from **My IP**
  - Custom TCP **8080** from **Anywhere (0.0.0.0/0)**  *(the twin calls this; it's protected by a bearer token below)*
- Launch. Note the **Public IPv4 address**.

## 2. Install + run (SSH in, 5 commands)
```bash
ssh -i your-key.pem ubuntu@<PUBLIC_IP>

# install docker
sudo apt-get update -y && sudo apt-get install -y docker.io git
sudo usermod -aG docker ubuntu && newgrp docker

# get the code (use your GitHub login / a PAT when prompted; repo is private)
git clone https://github.com/OTTOYARD/ottoq-intelligence.git
cd ottoq-intelligence

# build + run (restarts on reboot). Set a shared secret the twin will send.
docker build -t ottoq-intel .
docker run -d --restart unless-stopped -p 8080:8080 \
  -e OTTOQ_API_TOKEN='pick-a-long-random-string' \
  --name ottoq-intel ottoq-intel
```

## 3. Verify
```bash
curl http://<PUBLIC_IP>:8080/health
# -> {"ok":true,"service":"ottoq-intelligence","optimizers":["energy_mpc"]}
```
From your laptop browser: `http://<PUBLIC_IP>:8080/docs` shows the live API.

## 4. Send me
- the **Public IP** (or `http://<PUBLIC_IP>:8080`)
- the **OTTOQ_API_TOKEN** you chose

I wire the twin to call it (with the deterministic heuristic as automatic fallback if
it's ever unreachable) and run the closed-loop energy cert.

## Security / TLS
HTTP + bearer token is fine for dev + the pitch demo. For production I'll add auto-HTTPS
(Caddy reverse proxy, ~5 min, needs a subdomain) — trivial upgrade, not blocking now.

## GPU variant (FR-2 learned forecaster, this week — not yet)
Same flow, but: instance type **`g5.xlarge`**, AMI **"Deep Learning OSS Nvidia Driver AMI
(Ubuntu 22.04)"**, and the `Dockerfile.gpu` image (torch + neuralforecast). ~$1/hr, run
only while training/serving. I'll hand you the exact commands when we build FR-2.
