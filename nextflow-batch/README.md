# Nextflow Batch — Run Genomics Pipelines on Google Cloud Batch

Run [Nextflow](https://www.nextflow.io/) RNAseq pipelines on [Google Cloud Batch](https://cloud.google.com/batch) using a Vertex AI Workbench as the researcher environment.

## Prerequisites

- **L0** deployed — [fast-science-0-stellar-engine](https://github.com/WandLZhang/fast-science-0-stellar-engine)
- **L1** project created — [fast-science-1-researcher-lab](https://github.com/WandLZhang/fast-science-1-researcher-lab)
- Python ≥ 3.10, `gcloud` CLI authenticated

## What You Need From Your Admin

Your IT admin provides these values from the L0/L1 Terraform deployment:

| Env Var | What It Is | How Admin Gets It |
|---------|-----------|-------------------|
| `GCP_PROJECT_ID` | Your researcher project | L1 project factory output — the YAML filename with prefix |
| `HOST_PROJECT` | Prod spoke Shared VPC host | `cd fast/stages-aw/2-networking-* && terraform output host_project_ids` |
| `GCP_REGION` | Region matching L0 subnet | `cd fast/stages-aw/0-bootstrap && terraform output -json \| jq '.regions.value.primary'` |
| `SERVICE_ACCOUNT_NAME` | SA created by L1 project factory | Look in L1's `data/projects/<name>.yaml` under `service_accounts:` — the key is the SA name |

## Deploy

```bash
# Set the values your admin provided
export GCP_PROJECT_ID="<your-project-id>"          # ← from admin
export HOST_PROJECT="<prod-spoke-host-project>"    # ← from admin
export GCP_REGION="us-central1"                    # ← from admin
export SERVICE_ACCOUNT_NAME="<sa-from-l1-yaml>"    # ← from L1 YAML

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

1. **Enables workload-specific APIs** (batch, notebooks, compute — most pre-enabled by L1)
2. **Adds workload roles to L1 service account** (`batch.jobsEditor`, `batch.agentReporter`, `storage.admin`)
3. **Overrides org policy** (`compute.requireShieldedVm = false` — required for Batch worker VMs)
4. **Provisions a Vertex AI Workbench** (n1-standard-4, no public IP, on L0's shared subnet)
5. **Creates a GCS bucket** for pipeline scratch space
6. **Writes `nextflow.config`** — pre-configured for google-batch executor with private networking

The workbench uses the L0 Prod spoke shared subnet — all traffic flows through the central hub's NVAs and Cloud NAT. No standalone VPC or NAT is created.

## After Deployment

1. Open the Workbench in the [GCP Console](https://console.cloud.google.com/vertex-ai/workbench/instances)
2. Open a terminal and run:
   ```bash
   cd nextflow-workspace
   nextflow run nextflow-io/rnaseq-nf -c nextflow.config
   ```
3. Monitor jobs: `gcloud batch jobs list --location=$GCP_REGION`

## Pipeline Flow

```
Workbench (n1-standard-4, private IP)
    ├── nextflow.config (google-batch executor)
    ├── Launch: nextflow run nextflow-io/rnaseq-nf
    │   ├── INDEX → Cloud Batch job (spot VM)
    │   ├── FASTQC → Cloud Batch job (spot VM)
    │   ├── QUANT → Cloud Batch job (spot VM)
    │   └── MULTIQC → Cloud Batch job (spot VM)
    └── Results → gs://<project>-bucket/scratch/
```
