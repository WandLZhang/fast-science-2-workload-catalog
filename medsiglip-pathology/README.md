# MedSigLIP Pathology — Fine-tune Google's Medical Foundation Model

Fine-tune [MedSigLIP](https://cloud.google.com/vertex-ai/generative-ai/docs/model-garden/medsiglip-model-card) on the NCT-CRC-HE-100K pathology dataset using a Vertex AI Workbench with A100 GPU.

## Prerequisites

- **L0** deployed — [fast-science-0-stellar-engine](https://github.com/WandLZhang/fast-science-0-stellar-engine)
- **L1** project created — [fast-science-1-researcher-lab](https://github.com/WandLZhang/fast-science-1-researcher-lab)
- Python ≥ 3.10, `gcloud` CLI authenticated

## What You Need From Your Admin

Your IT admin provides these values from the L0/L1 Terraform deployment:

| Env Var | What It Is | How Admin Gets It |
|---------|-----------|-------------------|
| `GCP_PROJECT_ID` | Your researcher project | L1 project factory output — the YAML filename with prefix (e.g., `wzuniv-pathology-medsiglip`) |
| `HOST_PROJECT` | Prod spoke Shared VPC host | `cd fast/stages-aw/2-networking-* && terraform output host_project_ids` |
| `GCP_REGION` | Region matching L0 subnet | `cd fast/stages-aw/0-bootstrap && terraform output -json \| jq '.regions.value.primary'` |
| `SERVICE_ACCOUNT_NAME` | SA created by L1 project factory | Look in L1's `data/projects/<name>.yaml` under `service_accounts:` — the key is the SA name (e.g., `pathology-medsiglip-0`) |

## Deploy

```bash
# Set the values your admin provided
export GCP_PROJECT_ID="wzuniv-pathology-medsiglip"     # ← from admin
export HOST_PROJECT="wzuniv-prod-net-host"             # ← from admin
export GCP_REGION="us-central1"                        # ← from admin
export SERVICE_ACCOUNT_NAME="pathology-medsiglip-0"    # ← from L1 YAML (see above)

# Authenticate
gcloud auth login
gcloud auth application-default login
gcloud config set project $GCP_PROJECT_ID

# Install dependencies
pip install -r requirements.txt

# Run the deployment
python deploy.py
```

## What It Does

1. **Enables workload-specific APIs** (aiplatform, notebooks, healthcare — most pre-enabled by L1)
2. **Adds workload roles to L1 service account** (`aiplatform.user`, `notebooks.admin`, `storage.admin`)
3. **Overrides org policy** (`compute.requireShieldedVm = false` — required for NVIDIA GPU drivers)
4. **Provisions a Vertex AI Workbench** (A100 GPU, no public IP, on L0's shared subnet)
5. **Creates a GCS bucket** for pipeline artifacts

The workbench uses the L0 Prod spoke shared subnet — all internet traffic flows through the central hub's NVAs and Cloud NAT. No standalone VPC or NAT is created.

## Networking

```
                        INTERNET
                           │
                ┌──────────▼──────────┐
                │   L0 Hub VPC        │
                │   Cloud NAT + NVAs  │
                │   (egress control)  │
                └──────────┬──────────┘
                           │ VPC peering
                ┌──────────▼──────────────────────┐
                │  L0 Prod Spoke (Shared VPC Host) │
                │  Subnet: default-primary-region  │
                │  Private Google Access: ✓        │
                │                                  │
                │   ┌────────────────────────────┐ │
                │   │ Workbench VM (A100 GPU)     │ │
                │   │ Private IP only             │ │
                │   │ NIC → prod-spoke-0 subnet   │ │
                │   └─────────┬──────────────────┘ │
                │             │                    │
                │   ┌─────────▼──────────────────┐ │
                │   │ GCS Bucket (via PGA)        │ │
                │   └────────────────────────────┘ │
                └──────────────────────────────────┘

Outbound (pip, HuggingFace): VM → Spoke → Hub → NVA → Cloud NAT → Internet
Google APIs (GCS, Vertex AI): VM → Private Google Access → Google backbone
```

## After Deployment

1. Open the Workbench in the [GCP Console](https://console.cloud.google.com/vertex-ai/workbench/instances)
2. Install NVIDIA driver (say 'y' when prompted)
3. Open `medsiglip-workspace/MedSigLIP_Pathology_Pipeline.ipynb`
4. Run cells in order: install deps → load model → download dataset → fine-tune → evaluate

## Pipeline Flow

```
Workbench (A100 GPU, private IP)
    ├── Load MedSigLIP from HuggingFace
    ├── Download NCT-CRC-HE-100K + CRC-VAL-HE-7K
    ├── Zero-shot baseline evaluation
    ├── Fine-tune with contrastive loss (2 epochs)
    ├── Evaluate fine-tuned model
    └── Save results + checkpoints → GCS bucket
```
