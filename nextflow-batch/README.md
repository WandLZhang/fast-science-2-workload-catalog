# Nextflow Batch — Human RNAseq on Google Cloud Batch + BigQuery Public Data

Run [nf-core/rnaseq](https://nf-co.re/rnaseq) on [Google Cloud Batch](https://cloud.google.com/batch), then JOIN your gene expression results with TCGA, GTEx, and GENCODE in BigQuery — all from a single Vertex AI Workbench notebook.

## Prerequisites

- **L0** deployed — [fast-science-0-stellar-engine](https://github.com/WandLZhang/fast-science-0-stellar-engine)
- **L1** project created — [fast-science-1-researcher-lab](https://github.com/WandLZhang/fast-science-1-researcher-lab)
- Python ≥ 3.10, `gcloud` CLI authenticated

## What You Need From Your Admin

Your IT admin provides these values from the L0/L1 Terraform deployment:

| Env Var | What It Is | How Admin Gets It |
|---------|-----------|-------------------|
| `GCP_PROJECT_ID` | Your researcher project | L1 project factory output — the YAML filename with prefix |
| `HOST_PROJECT` | Network project from L0 Stage 2 | `from L0 Stage 2 terraform output (e.g. PREFIX-net-prod-0)` |
| `GCP_REGION` | Region matching L0 subnet | `cd fast/stages-aw/0-bootstrap && terraform output -json \| jq '.regions.value.primary'` |
| `SERVICE_ACCOUNT_NAME` | SA created by L1 project factory | Look in L1's `data/projects/<name>.yaml` under `service_accounts:` — the key is the SA name |

## What Your Admin Needs in the L1 Project YAML

The L1 project factory YAML ([fast-science-1-researcher-lab](https://github.com/WandLZhang/fast-science-1-researcher-lab)) needs workload-specific settings for Nextflow Batch to work on a Shared VPC. These are **admin-level** — the researcher's `deploy.py` should not need org policy or host-project permissions.

### Required `service_agent_subnet_iam`

```yaml
service_agent_subnet_iam:
  "us-central1/prod-default":
    - notebooks      # Workbench VM service agent
    - compute        # Compute Engine service agent
    - cloudbatch     # Cloud Batch service agent — creates worker VMs on the shared subnet
    - cloudservices  # Google APIs service agent — Batch uses this to create MIG VMs
```

If the project factory module doesn't recognize `cloudservices` as a type, grant it manually or via `network_subnet_users`:
>
> ```yaml
> network_subnet_users:
>   "us-central1/prod-default":
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

Include these in the `services:` list:
```yaml
services:
  # ... (standard Stellar Engine APIs) ...
  - batch.googleapis.com
  - bigquery.googleapis.com
  - notebooks.googleapis.com
  - aiplatform.googleapis.com
```

### Required IAM Roles (for deploy.py to grant to the workload SA)

The researcher's `deploy.py` grants these roles to the workload SA at the project level. The L1 project factory SA needs `roles/resourcemanager.projectIamAdmin` (or delegated grants) on the researcher project for this to work:

```yaml
# Roles granted by deploy.py to the workload SA:
# - roles/batch.jobsEditor
# - roles/batch.agentReporter
# - roles/bigquery.user         ← queries public datasets + creates jobs
# - roles/bigquery.dataEditor   ← loads pipeline results into BQ dataset
# - roles/storage.admin
# - roles/iam.serviceAccountUser
# - roles/aiplatform.user       ← Gemini/LLM API calls from notebook
```

---

## Deploy

```bash
# Set the values your admin provided
export GCP_PROJECT_ID="<your-project-id>"          # ← from admin
export HOST_PROJECT="<prefix>-net-prod-0"    # ← from admin
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

1. **Enables workload-specific APIs** (batch, notebooks, compute, aiplatform — most pre-enabled by L1)
2. **Adds workload roles to L1 service account** (`batch.jobsEditor`, `batch.agentReporter`, `storage.admin`)
3. **Overrides org policy** (`compute.requireShieldedVm = false` — required for Batch worker VMs)
4. **Provisions a Vertex AI Workbench** (n1-standard-4, no public IP, on L0's shared subnet)
5. **Creates a GCS bucket** for pipeline I/O
6. **Creates a BigQuery dataset** (`rnaseq_results` in US multi-region, co-located with ISB-CGC public datasets)
7. **Writes `nextflow.config`** — pre-configured for google-batch executor, no hardcoded machine type (Cloud Batch auto-selects)
8. **Creates `samplesheet.csv`** — GM12878 human test data for nf-core/rnaseq

The workbench uses the L0 Prod spoke shared subnet — all traffic flows through the central hub's NVAs and Cloud NAT. No standalone VPC or NAT is created.

## After Deployment

1. Open the Workbench in the [GCP Console](https://console.cloud.google.com/vertex-ai/workbench/instances)
2. Open a terminal and run:
   ```bash
   cd nextflow-workspace
   nextflow run nf-core/rnaseq -r 3.19.0 \
     --input samplesheet.csv \
     --outdir gs://<project>-bucket/results \
     --genome GRCh37 \
     --pseudo_aligner salmon \
     --skip_alignment
   ```
3. Monitor jobs: `gcloud batch jobs list --location=$GCP_REGION`
4. When complete, open `Explore_BigQuery_Public_Data.ipynb` and run all cells

## Pipeline Flow

```
Workbench (n1-standard-4, private IP)
    ├── nextflow.config (google-batch executor, no hardcoded machineType)
    ├── samplesheet.csv (GM12878 human test data)
    ├── Launch: nextflow run nf-core/rnaseq --genome GRCh37
    │   ├── PREPARE_GENOME → Cloud Batch (auto-sized VM)
    │   ├── FASTQC + TRIMGALORE → Cloud Batch (spot VMs)
    │   ├── SALMON_INDEX → Cloud Batch (auto-sized VM)
    │   ├── SALMON_QUANT → Cloud Batch (spot VM)
    │   └── MULTIQC → Cloud Batch (spot VM)
    ├── Results → gs://<project>-bucket/results/salmon/
    │   └── salmon.merged.gene_tpm.tsv (57K genes, ENSG IDs)
    │
    └── Explore_BigQuery_Public_Data.ipynb
        ├── 1. Load gene TPM into BigQuery
        ├── 2. JOIN GENCODE → annotate genes (symbols, types, chromosomes)
        ├── 3. JOIN TCGA → compare with 11K cancer samples across 33 types
        ├── 4. JOIN GTEx → compare with 54 normal tissues
        ├── 5. CORR() → which cancer type does my sample resemble?
        └── 6. Gemini → AI-powered biological summary
```

---

## Explore Results with BigQuery Public Data

After your pipeline completes, open `Explore_BigQuery_Public_Data.ipynb`. The notebook loads your gene expression results into BigQuery and JOINs them with public datasets — no data downloads required:

```
1. Load gene expression into BigQuery
   └── salmon.merged.gene_tpm.tsv → rnaseq_results.my_genes (US multi-region)

2. Annotate with GENCODE
   └── your_genes JOIN isb-cgc-bq.GENCODE.annotation_gtf_hg38_current
   └── Maps ENSG IDs → gene symbols, types, chromosomal locations

3. Compare with TCGA cancer data (11K samples, 33 cancer types)
   └── your_genes JOIN isb-cgc-bq.TCGA.RNAseq_hg38_gdc_current
   └── "Are my top genes also overexpressed in specific cancers?"

4. Compare with GTEx normal tissue (54 tissues)
   └── your_genes JOIN isb-cgc.GTEx_v7.gene_median_tpm
   └── "Is my expression abnormal compared to healthy tissue?"

5. Cancer type similarity
   └── CORR(my_tpm, tcga_avg_tpm) across 20K+ genes
   └── "Which TCGA cancer type does my sample most resemble?"

6. Ask Gemini to summarize findings
   └── Vertex AI Gemini SDK → biological pathway analysis
```

### Summary User Experience

| Step | What Happened | Google Cloud Service |
|------|--------------|---------------------|
| 1 | Provisioned researcher environment | **Vertex AI Workbench** (IAP auth, no public IP) |
| 2 | Ran human RNAseq pipeline | **Cloud Batch** + nf-core/rnaseq (spot VMs, GRCh37) |
| 3 | Loaded gene expression into BigQuery | **BigQuery** (US multi-region) |
| 4 | Annotated genes with GENCODE | **BigQuery** JOIN (gene symbols, types, locations) |
| 5 | Compared with 11K cancer samples | **BigQuery** JOIN with TCGA (33 cancer types) |
| 6 | Compared with normal tissue | **BigQuery** JOIN with GTEx (54 tissues) |
| 7 | Found most similar cancer type | **BigQuery** CORR() across 20K genes |
| 8 | Summarized findings with AI | **Vertex AI** Gemini SDK |

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
  --config=- <<< '{"taskGroups":[{"taskSpec":{"runnables":[{"script":{"text":"echo ok"}}],"computeResource":{"cpuMilli":1000,"memoryMib":512}},"taskCount":1}],"allocationPolicy":{"instances":[{"policy":{"machineType":"e2-micro","provisioningModel":"SPOT"}}],"serviceAccount":{"email":"<sa>@<project>.iam.gserviceaccount.com"},"network":{"networkInterfaces":[{"network":"projects/<host>/global/networks/prod","subnetwork":"projects/<host>/regions/<region>/subnetworks/<subnet>","noExternalIpAddress":true}]}},"logsPolicy":{"destination":"CLOUD_LOGGING"}}'
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
