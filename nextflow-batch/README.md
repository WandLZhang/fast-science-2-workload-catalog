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

## What Your Admin Needs in the L1 Project YAML

The L1 project factory YAML ([fast-science-1-researcher-lab](https://github.com/WandLZhang/fast-science-1-researcher-lab)) needs workload-specific settings for Nextflow Batch to work on a Shared VPC. These are **admin-level** — the researcher's `deploy.py` should not need org policy or host-project permissions.

### Required `service_agent_subnet_iam`

```yaml
service_agent_subnet_iam:
  "us-central1/default-primary-region":
    - notebooks      # Workbench VM service agent
    - compute        # Compute Engine service agent
    - cloudbatch     # Cloud Batch service agent — creates worker VMs on the shared subnet
    - cloudservices  # Google APIs service agent — Batch uses this to create MIG VMs
```

If the project factory module doesn't recognize `cloudservices` as a type, grant it manually or via `network_subnet_users`:
>
> ```yaml
> network_subnet_users:
>   "us-central1/default-primary-region":
>     - serviceAccount:<project-number>@cloudservices.gserviceaccount.com
> ```

### Required `org_policies`

```yaml
org_policies:
  compute.requireShieldedVm:
    rules:
      - enforce: false
  compute.trustedImageProjects:
    inherit_from_parent: true
    rules:
      - allow:
          values:
            - projects/batch-custom-image
```

### Required APIs

Include `batch.googleapis.com` and `notebooks.googleapis.com` in the `services:` list.

---

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

---

## Admin Hints — Shared VPC Troubleshooting

### Batch jobs stuck in `SCHEDULED_PENDING_FAILED`

**Symptom:** Jobs go QUEUED → SCHEDULED → SCHEDULED_PENDING_FAILED → FAILED. The status event says:
```
CODE_GCE_PERMISSION_DENIED: Required 'compute.subnetworks.use' permission for
'projects/<host-project>/regions/<region>/subnetworks/<subnet>'
(when acting as '<project-number>@cloudservices.gserviceaccount.com')
```

**Cause:** Cloud Batch creates worker VMs inside a Managed Instance Group. The MIG is created by the **Google APIs service agent** (`<project-number>@cloudservices.gserviceaccount.com`), which is a different principal from the Batch service agent. This agent needs `compute.networkUser` on the Shared VPC subnet in the host project.

**Fix (ad-hoc):**
```bash
# Get your workload project number
PROJECT_NUMBER=$(gcloud projects describe <your-project-id> --format="value(projectNumber)")

# Grant subnet IAM
gcloud compute networks subnets add-iam-policy-binding <subnet-name> \
  --project=<host-project> \
  --region=<region> \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudservices.gserviceaccount.com" \
  --role="roles/compute.networkUser"
```

**Fix (codified in L1):** Add `cloudservices` to `service_agent_subnet_iam` in the project factory YAML (see above).

**Verify:** Submit a test job:
```bash
gcloud batch jobs submit test-fix \
  --project=<your-project-id> --location=<region> \
  --config=- <<< '{"taskGroups":[{"taskSpec":{"runnables":[{"script":{"text":"echo ok"}}],"computeResource":{"cpuMilli":1000,"memoryMib":512}},"taskCount":1}],"allocationPolicy":{"instances":[{"policy":{"machineType":"e2-micro","provisioningModel":"SPOT"}}],"serviceAccount":{"email":"<sa>@<project>.iam.gserviceaccount.com"},"network":{"networkInterfaces":[{"network":"projects/<host>/global/networks/prod-spoke-0","subnetwork":"projects/<host>/regions/<region>/subnetworks/<subnet>","noExternalIpAddress":true}]}},"logsPolicy":{"destination":"CLOUD_LOGGING"}}'
```

### Why this doesn't happen outside Shared VPC

In standalone projects (no L0/L1), Batch VMs use the default network in the same project. The Google APIs service agent has implicit access to same-project networks. Shared VPC is a cross-project boundary that requires explicit IAM grants for every agent that creates VMs.

### Complete list of service agents that need subnet IAM for Batch workloads

| Agent | Email Pattern | Why |
|-------|--------------|-----|
| Google APIs | `{number}@cloudservices.gserviceaccount.com` | Creates MIG for Batch worker VMs |
| Compute Engine | `service-{number}@compute-system.iam.gserviceaccount.com` | Creates individual VM instances |
| Cloud Batch | `service-{number}@gcp-sa-cloudbatch.iam.gserviceaccount.com` | Manages Batch job lifecycle |
| Notebooks | `service-{number}@gcp-sa-notebooks.iam.gserviceaccount.com` | Creates Workbench VM |
