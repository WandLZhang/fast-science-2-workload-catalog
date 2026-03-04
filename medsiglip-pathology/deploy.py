"""
@file deploy.py
@brief MedSigLIP Pathology workload deployment script

@details Provisions GCP infrastructure for MedSigLIP fine-tuning:
- Workload-specific APIs and service account
- Org policy override for GPU drivers
- Vertex AI Workbench (A100) on L0 shared subnet
- GCS bucket for pipeline artifacts

Requires: GCP_PROJECT_ID, HOST_PROJECT, GCP_REGION env vars (from admin)

@author Willis Zhang
@date 2026-01-30
"""

import json
import os
import sys
import time

# GCP Libraries
from google.cloud import storage
from googleapiclient import discovery
from google.auth import default
from google.api_core import exceptions as gcp_exceptions

# ─── Configuration ────────────────────────────────────────────────────────────
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
if not PROJECT_ID:
    print("ERROR: Set GCP_PROJECT_ID environment variable (from L1 project factory)")
    print("  export GCP_PROJECT_ID='<prefix>-dept-researcher'")
    sys.exit(1)

BUCKET_NAME = f"{PROJECT_ID}-bucket"
# L1 project factory creates this SA — we reuse it and add workload-specific roles
SERVICE_ACCOUNT_NAME = os.environ.get("SERVICE_ACCOUNT_NAME", "pathology-medsiglip-0")

# L0 networking — use the Prod spoke shared subnet (set by Central IT)
HOST_PROJECT = os.environ.get("HOST_PROJECT", "")
REGION = os.environ.get("GCP_REGION", "us-central1")

# Zone fallback list for A100 — tries us-central1 first, then us-west1
# us-west1-b has A100 capacity when us-central1 is stocked out
# Note: us-central1-f has capacity but Notebooks API doesn't support it
PREFERRED_ZONES = [
    'us-central1-b', 'us-central1-a', 'us-central1-c',
    'us-west1-b', 'us-west1-a', 'us-west1-c',
]
ZONE = f"{REGION}-b"
WORKBENCH_INSTANCE_NAME = os.environ.get("WORKBENCH_INSTANCE_NAME", "medsiglip-researcher-workbench")


# ─── Logging helpers ─────────────────────────────────────────────────────────

def log_msg(msg: str, msg_type: str = "info"):
    """Print a log message with type prefix."""
    print(f"[{msg_type.upper():>7}] {msg}")


def step_complete():
    """Mark step as complete."""
    print("[SUCCESS] ✓ Done")


def step_error(msg: str):
    """Mark step as error."""
    print(f"[  ERROR] ✗ {msg}")


# ─── Deployment step executors ────────────────────────────────────────────────

def execute_enable_apis():
    """Enable required GCP APIs using Service Usage API."""
    log_msg("Enabling Vertex AI, Compute, Healthcare, and IAM APIs...")

    try:
        credentials, project = default()
        service = discovery.build('serviceusage', 'v1', credentials=credentials)

        apis = [
            'aiplatform.googleapis.com',
            'compute.googleapis.com',
            'healthcare.googleapis.com',
            'iam.googleapis.com',
            'cloudresourcemanager.googleapis.com',
            'orgpolicy.googleapis.com',
            'notebooks.googleapis.com',
            'storage.googleapis.com',
        ]

        for api in apis:
            log_msg(f"  Enabling {api}...")
            try:
                service.services().enable(
                    name=f'projects/{PROJECT_ID}/services/{api}'
                ).execute()
                log_msg(f"  ✓ {api} enabled", "success")
            except Exception as e:
                if "already enabled" in str(e).lower():
                    log_msg(f"  ✓ {api} already enabled", "info")
                else:
                    log_msg(f"  ⚠ {api}: {str(e)[:100]}", "info")

        step_complete()
    except Exception as e:
        step_error(str(e))


def execute_create_service_account():
    """Create service account using IAM API."""
    log_msg(f"Creating service account: {SERVICE_ACCOUNT_NAME}...")

    try:
        credentials, project = default()
        service = discovery.build('iam', 'v1', credentials=credentials)

        sa_email = f"{SERVICE_ACCOUNT_NAME}@{PROJECT_ID}.iam.gserviceaccount.com"

        try:
            service.projects().serviceAccounts().get(
                name=f"projects/{PROJECT_ID}/serviceAccounts/{sa_email}"
            ).execute()
            log_msg(f"  Service account already exists: {sa_email}", "info")
        except Exception:
            service.projects().serviceAccounts().create(
                name=f"projects/{PROJECT_ID}",
                body={
                    'accountId': SERVICE_ACCOUNT_NAME,
                    'serviceAccount': {
                        'displayName': 'MedSigLIP Pipeline Service Account'
                    }
                }
            ).execute()
            log_msg(f"  Created: {sa_email}", "success")

        step_complete()
    except Exception as e:
        step_error(str(e))


def execute_iam_roles():
    """Add IAM roles to service account."""
    log_msg("Adding IAM roles to service account...")

    try:
        credentials, project = default()
        service = discovery.build('cloudresourcemanager', 'v1', credentials=credentials)

        sa_email = f"{SERVICE_ACCOUNT_NAME}@{PROJECT_ID}.iam.gserviceaccount.com"
        member = f"serviceAccount:{sa_email}"

        roles = [
            'roles/iam.serviceAccountUser',
            'roles/aiplatform.user',
            'roles/notebooks.admin',
            'roles/logging.viewer',
            'roles/storage.admin',
        ]

        policy = service.projects().getIamPolicy(
            resource=PROJECT_ID,
            body={}
        ).execute()

        for role in roles:
            log_msg(f"  Adding {role}...")

            binding_exists = False
            for binding in policy.get('bindings', []):
                if binding['role'] == role:
                    if member not in binding['members']:
                        binding['members'].append(member)
                    binding_exists = True
                    break

            if not binding_exists:
                policy.setdefault('bindings', []).append({
                    'role': role,
                    'members': [member]
                })

        service.projects().setIamPolicy(
            resource=PROJECT_ID,
            body={'policy': policy}
        ).execute()

        for role in roles:
            log_msg(f"  ✓ {role} granted", "success")

        step_complete()
    except Exception as e:
        step_error(str(e))


def execute_configure_org_policies():
    """
    Override org policy constraints that block GPU workbench provisioning.

    compute.requireShieldedVm: When enforced (inherited from org), requires
    Secure Boot on all VMs. Secure Boot blocks NVIDIA proprietary GPU drivers
    because they are unsigned kernel modules. We override this at the project
    level to allow non-secure-boot VMs for A100 GPU workbenches.
    """
    log_msg("Configuring org policy overrides for GPU workbench...")

    try:
        credentials, project = default()
        orgpolicy_service = discovery.build('orgpolicy', 'v2', credentials=credentials)

        policy_name = f"projects/{PROJECT_ID}/policies/compute.requireShieldedVm"

        log_msg("  Disabling compute.requireShieldedVm (required for NVIDIA GPU drivers)...")

        try:
            policy_body = {
                "name": policy_name,
                "spec": {
                    "rules": [{"enforce": False}]
                }
            }

            try:
                orgpolicy_service.projects().policies().create(
                    parent=f"projects/{PROJECT_ID}",
                    body=policy_body
                ).execute()
                log_msg("  ✓ compute.requireShieldedVm overridden (enforce: false)", "success")
            except Exception as create_err:
                create_str = str(create_err)
                if '409' in create_str or 'already exists' in create_str.lower():
                    orgpolicy_service.projects().policies().patch(
                        name=policy_name,
                        body=policy_body
                    ).execute()
                    log_msg("  ✓ compute.requireShieldedVm updated (enforce: false)", "success")
                else:
                    raise create_err

            log_msg("  NVIDIA unsigned kernel modules can now load on VMs", "info")
        except Exception as e:
            err_str = str(e)
            if 'PERMISSION_DENIED' in err_str or '403' in err_str:
                log_msg("  ⚠ Permission denied — need orgpolicy.policyAdmin role", "error")
                log_msg(f"  Run: gcloud org-policies set-policy --project={PROJECT_ID} with enforce:false", "info")
                step_error("Missing orgpolicy.policyAdmin permission")
                return
            else:
                log_msg(f"  ⚠ Org policy override: {err_str[:120]}", "error")
                raise e

        step_complete()
    except Exception as e:
        step_error(str(e))


def execute_provision_workbench():
    """
    Provision a Vertex AI Workbench instance for researchers.

    Uses the Notebooks API v2 to create a Workbench Instance.
    Tries zones from PREFERRED_ZONES in order; on STOCKOUT, skips to next zone.
    Updates global REGION/ZONE so downstream output matches the resolved zone.
    If instance already exists in the current ZONE, reports its URL.
    """
    global REGION, ZONE

    log_msg(f"Provisioning Vertex AI Workbench: {WORKBENCH_INSTANCE_NAME}...")

    try:
        credentials, project = default()

        # Enable the Notebooks API if not already enabled
        log_msg("  Enabling notebooks.googleapis.com API...")
        try:
            service_usage = discovery.build('serviceusage', 'v1', credentials=credentials)
            service_usage.services().enable(
                name=f'projects/{PROJECT_ID}/services/notebooks.googleapis.com'
            ).execute()
            log_msg("  ✓ Notebooks API enabled", "success")
        except Exception as e:
            if "already enabled" in str(e).lower():
                log_msg("  ✓ Notebooks API already enabled", "info")
            else:
                log_msg(f"  ⚠ Notebooks API: {str(e)[:80]}", "info")

        notebooks_service = discovery.build('notebooks', 'v2', credentials=credentials)
        workbench_url = f"https://console.cloud.google.com/vertex-ai/workbench/instances?project={PROJECT_ID}"

        # Check if instance already exists in the current default zone
        instance_name = f"projects/{PROJECT_ID}/locations/{ZONE}/instances/{WORKBENCH_INSTANCE_NAME}"
        log_msg(f"  Checking for existing instance in {ZONE}...")
        try:
            instance = notebooks_service.projects().locations().instances().get(
                name=instance_name
            ).execute()
            state = instance.get('state', 'UNKNOWN')
            log_msg(f"  ✓ Workbench instance already exists (state: {state})", "info")
            if 'proxyUri' in instance:
                log_msg(f"  JupyterLab URL: {instance['proxyUri']}", "success")
            log_msg(f"  Console: {workbench_url}", "info")
            step_complete()
            return
        except Exception as e:
            err_str = str(e)
            if 'notFound' in err_str.lower() or '404' in err_str:
                log_msg("  Instance not found, will create new one...", "info")
            elif 'SERVICE_DISABLED' in err_str or 'has not been used' in err_str:
                log_msg("  ⏳ Notebooks API propagating, proceeding to create...", "info")
                time.sleep(15)
                notebooks_service = discovery.build('notebooks', 'v2', credentials=credentials)
            else:
                raise e

        # --- Zone fallback loop: try each zone in PREFERRED_ZONES ---
        sa_email = f"{SERVICE_ACCOUNT_NAME}@{PROJECT_ID}.iam.gserviceaccount.com"

        startup_script = f'''#!/bin/bash
set -e
echo "=== MedSigLIP A100 Workbench Setup ==="
nvidia-smi || echo "WARNING: nvidia-smi not found, GPU drivers may need install"
mkdir -p /home/jupyter/medsiglip-workspace

# Create the MedSigLIP pathology notebook
cat > /home/jupyter/medsiglip-workspace/MedSigLIP_Pathology_Pipeline.ipynb << 'NOTEBOOK'
{{
  "cells": [
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["# MedSigLIP Pathology Pipeline on A100\\n", "\\n", "This notebook runs the complete MedSigLIP pipeline on GPU:\\n", "1. Load model from HuggingFace\\n", "2. Download NCT-CRC-HE-100K dataset\\n", "3. Zero-shot baseline on CRC-VAL-HE-7K\\n", "4. Fine-tune with HuggingFace Trainer\\n", "5. Evaluate & compare pretrained vs fine-tuned\\n", "\\n", "Based on: https://github.com/google-health/medsiglip/blob/main/notebooks/fine_tune_with_hugging_face.ipynb"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 1: Setup & GPU Verification"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["!pip install -q --upgrade torch torchvision --index-url https://download.pytorch.org/whl/cu124\\n", "!pip install -q --force-reinstall \\"transformers==4.45.2\\"\\n", "!pip install -q datasets huggingface_hub accelerate evaluate\\n", "!pip install -q google-cloud-aiplatform google-cloud-storage\\n", "!pip install -q scikit-learn matplotlib pillow tqdm\\n", "!pip install -q sentencepiece protobuf\\n", "\\n", "# After finishes, restart kernel"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["import torch\\n", "print(f\\"GPU: {{torch.cuda.get_device_name(0)}}\\")", "\\n", "print(f\\"VRAM: {{torch.cuda.get_device_properties(0).total_memory / 1e9:.1f}} GB\\")", "\\n", "assert torch.cuda.is_available(), \\"No GPU found!\\"\\n", "\\n", "PROJECT_ID = \\"{PROJECT_ID}\\"\\n", "BUCKET_NAME = \\"{BUCKET_NAME}\\""]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 2: Create GCS Bucket"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["!gcloud storage buckets describe gs://{BUCKET_NAME} 2>/dev/null || gcloud storage buckets create gs://{BUCKET_NAME} --project={PROJECT_ID}\\n", "print(f\\"Bucket ready: gs://{BUCKET_NAME}\\")"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 3: HuggingFace Auth & Load MedSigLIP"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["from huggingface_hub import get_token\\n", "if get_token() is None:\\n", "    from huggingface_hub import notebook_login\\n", "    notebook_login()\\n", "\\n", "from transformers import SiglipProcessor, SiglipModel\\n", "\\n", "model_id = \\"google/medsiglip-448\\"\\n", "model = SiglipModel.from_pretrained(model_id)\\n", "processor = SiglipProcessor.from_pretrained(model_id)\\n", "print(f\\"Loaded {{model_id}}\\")\\n", "\\n", "import json, datetime\\n", "from google.cloud import storage as gcs\\n", "client = gcs.Client()\\n", "bucket = client.bucket(\\"{BUCKET_NAME}\\")\\n", "bucket.blob(\\"status/load-model.json\\").upload_from_string(\\n", "    json.dumps({{\\"step\\": \\"load-model\\", \\"status\\": \\"complete\\", \\"timestamp\\": datetime.datetime.now().isoformat()}})\\n", ")"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 4: Download NCT-CRC-HE-100K + CRC-VAL-HE-7K"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["!wget -nc -q \\"https://zenodo.org/records/1214456/files/NCT-CRC-HE-100K.zip\\"\\n", "!wget -nc -q \\"https://zenodo.org/records/1214456/files/CRC-VAL-HE-7K.zip\\"\\n", "!unzip -qn NCT-CRC-HE-100K.zip\\n", "!unzip -qn CRC-VAL-HE-7K.zip\\n", "print(\\"Datasets downloaded and extracted.\\")\\n", "\\n", "bucket.blob(\\"status/download-dataset.json\\").upload_from_string(\\n", "    json.dumps({{\\"step\\": \\"download-dataset\\", \\"status\\": \\"complete\\", \\"timestamp\\": datetime.datetime.now().isoformat()}})\\n", ")"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 5: Prepare Training Dataset"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["from datasets import load_dataset\\n", "from torchvision.transforms import Compose, Resize, ToTensor, Normalize, InterpolationMode\\n", "\\n", "train_size = 9000\\n", "validation_size = 1000\\n", "\\n", "data = load_dataset(\\"./NCT-CRC-HE-100K\\", split=\\"train\\")\\n", "data = data.train_test_split(train_size=train_size, test_size=validation_size, shuffle=True, seed=42)\\n", "data[\\"validation\\"] = data.pop(\\"test\\")\\n", "\\n", "TISSUE_CLASSES = [\\n", "    \\"adipose\\", \\"background\\", \\"debris\\", \\"lymphocytes\\", \\"mucus\\",\\n", "    \\"smooth muscle\\", \\"normal colon mucosa\\", \\"cancer-associated stroma\\",\\n", "    \\"colorectal adenocarcinoma epithelium\\"\\n", "]\\n", "\\n", "size = processor.image_processor.size[\\"height\\"]\\n", "mean = processor.image_processor.image_mean\\n", "std = processor.image_processor.image_std\\n", "\\n", "_transform = Compose([\\n", "    Resize((size, size), interpolation=InterpolationMode.BILINEAR),\\n", "    ToTensor(),\\n", "    Normalize(mean=mean, std=std),\\n", "])\\n", "\\n", "def preprocess(examples):\\n", "    pixel_values = [_transform(image.convert(\\"RGB\\")) for image in examples[\\"image\\"]]\\n", "    captions = [TISSUE_CLASSES[label] for label in examples[\\"label\\"]]\\n", "    inputs = processor.tokenizer(captions, max_length=64, padding=\\"max_length\\", truncation=True, return_attention_mask=True)\\n", "    inputs[\\"pixel_values\\"] = pixel_values\\n", "    return inputs\\n", "\\n", "data = data.map(preprocess, batched=True, remove_columns=[\\"image\\", \\"label\\"])\\n", "print(f\\"Train: {{len(data['train'])}}, Val: {{len(data['validation'])}}\\")" ]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 6: Zero-Shot Baseline (Pretrained Model)"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["import io, evaluate\\n", "from PIL import Image\\n", "\\n", "test_data = load_dataset(\\"./CRC-VAL-HE-7K\\", split=\\"train\\")\\n", "test_data = test_data.shuffle(seed=42).select(range(1000))\\n", "test_batches = test_data.batch(batch_size=64)\\n", "\\n", "accuracy_metric = evaluate.load(\\"accuracy\\")\\n", "f1_metric = evaluate.load(\\"f1\\")\\n", "REFERENCES = test_data[\\"label\\"]\\n", "\\n", "def compute_metrics(predictions):\\n", "    metrics = {{}}\\n", "    metrics.update(accuracy_metric.compute(predictions=predictions, references=REFERENCES))\\n", "    metrics.update(f1_metric.compute(predictions=predictions, references=REFERENCES, average=\\"weighted\\"))\\n", "    return metrics\\n", "\\n", "pt_model = SiglipModel.from_pretrained(model_id, device_map=\\"auto\\")\\n", "pt_predictions = []\\n", "for batch in test_batches:\\n", "    images = [Image.open(io.BytesIO(image[\\"bytes\\"])) for image in batch[\\"image\\"]]\\n", "    inputs = processor(text=TISSUE_CLASSES, images=images, padding=\\"max_length\\", return_tensors=\\"pt\\").to(\\"cuda\\")\\n", "    with torch.no_grad():\\n", "        outputs = pt_model(**inputs)\\n", "    pt_predictions.extend(outputs.logits_per_image.argmax(axis=1).tolist())\\n", "\\n", "pt_metrics = compute_metrics(pt_predictions)\\n", "print(f\\"Pretrained baseline: {{pt_metrics}}\\")"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 7: Fine-Tune with Contrastive Loss"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["from transformers import TrainingArguments, Trainer\\n", "\\n", "def collate_fn(examples):\\n", "    pixel_values = torch.tensor([ex[\\"pixel_values\\"] for ex in examples])\\n", "    input_ids = torch.tensor([ex[\\"input_ids\\"] for ex in examples])\\n", "    attention_mask = torch.tensor([ex[\\"attention_mask\\"] for ex in examples])\\n", "    return {{\\"pixel_values\\": pixel_values, \\"input_ids\\": input_ids, \\"attention_mask\\": attention_mask, \\"return_loss\\": True}}\\n", "\\n", "training_args = TrainingArguments(\\n", "    output_dir=\\"medsiglip-448-ft-crc100k\\",\\n", "    num_train_epochs=2,\\n", "    per_device_train_batch_size=8,\\n", "    per_device_eval_batch_size=8,\\n", "    gradient_accumulation_steps=8,\\n", "    logging_steps=50,\\n", "    save_strategy=\\"epoch\\",\\n", "    eval_strategy=\\"steps\\",\\n", "    eval_steps=50,\\n", "    learning_rate=1e-4,\\n", "    weight_decay=0.01,\\n", "    warmup_steps=5,\\n", "    lr_scheduler_type=\\"cosine\\",\\n", "    push_to_hub=False,\\n", "    report_to=\\"tensorboard\\",\\n", ")\\n", "\\n", "trainer = Trainer(\\n", "    model=model,\\n", "    args=training_args,\\n", "    train_dataset=data[\\"train\\"],\\n", "    eval_dataset=data[\\"validation\\"].shuffle().select(range(200)),\\n", "    data_collator=collate_fn,\\n", ")\\n", "\\n", "trainer.train()\\n", "trainer.save_model()\\n", "print(\\"Fine-tuning complete! Model saved to medsiglip-448-ft-crc100k/\\")\\n", "\\n", "bucket.blob(\\"status/fine-tune.json\\").upload_from_string(\\n", "    json.dumps({{\\"step\\": \\"fine-tune\\", \\"status\\": \\"complete\\", \\"timestamp\\": datetime.datetime.now().isoformat()}})\\n", ")"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 8: Evaluate Fine-Tuned Model & Compare"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["test_data = load_dataset(\\"./CRC-VAL-HE-7K\\", split=\\"train\\")\\n", "test_data = test_data.shuffle(seed=42).select(range(1000))\\n", "test_batches = test_data.batch(batch_size=64)\\n", "REFERENCES = test_data[\\"label\\"]\\n", "\\n", "ft_model = SiglipModel.from_pretrained(\\"medsiglip-448-ft-crc100k\\", device_map=\\"auto\\")\\n", "\\n", "ft_predictions = []\\n", "for batch in test_batches:\\n", "    images = [Image.open(io.BytesIO(image[\\"bytes\\"])) for image in batch[\\"image\\"]]\\n", "    inputs = processor(text=TISSUE_CLASSES, images=images, padding=\\"max_length\\", return_tensors=\\"pt\\").to(\\"cuda\\")\\n", "    with torch.no_grad():\\n", "        outputs = ft_model(**inputs)\\n", "    ft_predictions.extend(outputs.logits_per_image.argmax(axis=1).tolist())\\n", "\\n", "ft_metrics = compute_metrics(ft_predictions)\\n", "print(f\\"Pretrained: {{pt_metrics}}\\")\\n", "print(f\\"Fine-tuned: {{ft_metrics}}\\")\\n", "print(f\\"Accuracy improvement: {{ft_metrics['accuracy'] - pt_metrics['accuracy']:.4f}}\\")"]
    }},
    {{
      "cell_type": "markdown",
      "metadata": {{}},
      "source": ["## Step 9: Save Results to GCS"]
    }},
    {{
      "cell_type": "code",
      "execution_count": null,
      "metadata": {{}},
      "outputs": [],
      "source": ["import json as _json\\n", "results = {{\\"pretrained\\": pt_metrics, \\"fine_tuned\\": ft_metrics}}\\n", "with open(\\"evaluation_results.json\\", \\"w\\") as f:\\n", "    _json.dump(results, f, indent=2)\\n", "\\n", "!gsutil cp evaluation_results.json gs://{BUCKET_NAME}/results/\\n", "!gsutil cp -r medsiglip-448-ft-crc100k/ gs://{BUCKET_NAME}/medsiglip-finetune/\\n", "print(f\\"Results saved to gs://{BUCKET_NAME}/results/\\")\\n", "print(f\\"Checkpoints saved to gs://{BUCKET_NAME}/medsiglip-finetune/\\")"]
    }}
  ],
  "metadata": {{
    "kernelspec": {{
      "display_name": "Python 3",
      "language": "python",
      "name": "python3"
    }}
  }},
  "nbformat": 4,
  "nbformat_minor": 4
}}
NOTEBOOK

chown -R jupyter:jupyter /home/jupyter/medsiglip-workspace
echo "=== Setup complete. Open medsiglip-workspace/ in JupyterLab ==="
'''

        log_msg(f"  Trying {len(PREFERRED_ZONES)} zones for A100 availability...", "info")

        for zone_candidate in PREFERRED_ZONES:
            candidate_region = zone_candidate.rsplit('-', 1)[0]

            instance_body = {
                'gceSetup': {
                    'machineType': 'a2-highgpu-1g',
                    'acceleratorConfigs': [{'type': 'NVIDIA_TESLA_A100', 'coreCount': '1'}],
                    'serviceAccounts': [{'email': sa_email, 'scopes': ['https://www.googleapis.com/auth/cloud-platform']}],
                    'networkInterfaces': [{
                        'network': f'projects/{HOST_PROJECT}/global/networks/prod-spoke-0' if HOST_PROJECT else f'projects/{PROJECT_ID}/global/networks/default',
                        'subnet': (
                            f'projects/{HOST_PROJECT}/regions/{candidate_region}/subnetworks/gpu-west1'
                            if HOST_PROJECT and candidate_region == 'us-west1'
                            else f'projects/{HOST_PROJECT}/regions/{candidate_region}/subnetworks/default-primary-region'
                            if HOST_PROJECT
                            else f'projects/{PROJECT_ID}/regions/{candidate_region}/subnetworks/default'
                        ),
                        'nicType': 'VIRTIO_NET'
                    }],
                    'disablePublicIp': True,
                    'metadata': {'startup-script': startup_script, 'proxy-mode': 'service_account'},
                    'bootDisk': {'diskSizeGb': '300', 'diskType': 'PD_SSD'},
                    'vmImage': {'project': 'cloud-notebooks-managed', 'name': 'workbench-instances-v20260122'},
                    'shieldedInstanceConfig': {'enableSecureBoot': False, 'enableVtpm': True, 'enableIntegrityMonitoring': True}
                }
            }

            log_msg(f"  → Trying zone: {zone_candidate} (region: {candidate_region})...", "info")

            try:
                operation = notebooks_service.projects().locations().instances().create(
                    parent=f"projects/{PROJECT_ID}/locations/{zone_candidate}",
                    instanceId=WORKBENCH_INSTANCE_NAME,
                    body=instance_body
                ).execute()
            except Exception as e:
                err_str = str(e)
                if 'STOCKOUT' in err_str or 'does not have enough resources' in err_str:
                    log_msg(f"  ✗ {zone_candidate}: STOCKOUT — no A100 capacity", "error")
                    continue
                elif 'already exists' in err_str.lower():
                    log_msg(f"  ✓ Instance already exists in {zone_candidate}", "info")
                    REGION = candidate_region
                    ZONE = zone_candidate
                    log_msg(f"  Console: {workbench_url}", "info")
                    step_complete()
                    return
                else:
                    log_msg(f"  ✗ {zone_candidate}: {err_str[:120]}", "error")
                    continue

            # Creation request accepted — update globals to this zone
            REGION = candidate_region
            ZONE = zone_candidate
            log_msg(f"  ✓ Creation accepted in {zone_candidate}!", "success")
            log_msg(f"  Machine: a2-highgpu-1g (A100 40GB GPU), Zone: {ZONE}", "info")

            operation_name = operation.get('name')
            log_msg(f"  Operation: {operation_name.split('/')[-1]}", "info")

            # Poll for operation completion
            instance_name = f"projects/{PROJECT_ID}/locations/{ZONE}/instances/{WORKBENCH_INSTANCE_NAME}"
            max_wait = 600
            poll_interval = 15
            elapsed = 0

            while elapsed < max_wait:
                op_result = notebooks_service.projects().locations().operations().get(
                    name=operation_name
                ).execute()

                if op_result.get('done'):
                    if 'error' in op_result:
                        err_msg = op_result['error'].get('message', 'Unknown error')
                        if 'STOCKOUT' in err_msg or 'does not have enough resources' in err_msg:
                            log_msg(f"  ✗ {zone_candidate}: STOCKOUT during provisioning", "error")
                            break
                        step_error(f"Failed in {zone_candidate}: {err_msg}")
                        return

                    log_msg("  ✓ Workbench instance created successfully!", "success")

                    instance = notebooks_service.projects().locations().instances().get(
                        name=instance_name
                    ).execute()

                    if 'proxyUri' in instance:
                        log_msg(f"  JupyterLab URL: {instance['proxyUri']}", "success")

                    log_msg(f"  Console: {workbench_url}", "info")
                    step_complete()
                    return

                elapsed += poll_interval
                log_msg(f"  Provisioning in {zone_candidate}... ({elapsed}s elapsed)", "info")
                time.sleep(poll_interval)
            else:
                log_msg(f"  ⚠ Still provisioning in {zone_candidate} (check console)", "info")
                log_msg(f"  Console: {workbench_url}", "info")
                step_complete()
                return

        # All zones exhausted
        step_error(f"All {len(PREFERRED_ZONES)} zones stocked out. No A100 capacity available.")

    except Exception as e:
        print(f"[ERROR] Workbench provisioning failed: {str(e)}")
        step_error(str(e))


def execute_create_bucket():
    """Create GCS bucket using google-cloud-storage."""
    log_msg(f"Creating GCS bucket: gs://{BUCKET_NAME}...")

    try:
        client = storage.Client(project=PROJECT_ID)

        try:
            bucket = client.get_bucket(BUCKET_NAME)
            log_msg(f"  Bucket already exists: gs://{BUCKET_NAME}", "info")
        except gcp_exceptions.NotFound:
            bucket = client.create_bucket(BUCKET_NAME, location=REGION)
            log_msg(f"  Created bucket: gs://{BUCKET_NAME} in {REGION}", "success")

        log_msg(f"  Location: {bucket.location}", "info")
        step_complete()
    except Exception as e:
        step_error(str(e))


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"""
    ╔═══════════════════════════════════════════════════════════════╗
    ║     MedSigLIP Pathology — Workload Deployment                 ║
    ╠═══════════════════════════════════════════════════════════════╣
    ║  Project:     {PROJECT_ID:<45} ║
    ║  Host VPC:    {HOST_PROJECT or '(not set — using standalone)':<45} ║
    ║  Region:      {REGION:<45} ║
    ║  Bucket:      gs://{BUCKET_NAME:<42} ║
    ╚═══════════════════════════════════════════════════════════════╝
    """)

    steps = [
        ('enable-apis', execute_enable_apis),
        ('create-sa', execute_create_service_account),
        ('iam-roles', execute_iam_roles),
        ('org-policies', execute_configure_org_policies),
        ('provision-workbench', execute_provision_workbench),
        ('storage-bucket', execute_create_bucket),
    ]

    for step_name, step_fn in steps:
        print(f"\n{'='*60}")
        print(f"Step: {step_name}")
        print(f"{'='*60}")
        step_fn()
        print()

    print("\n✅ Deployment complete!")
    print(f"   Project: {PROJECT_ID}")
    print(f"   Workbench: https://console.cloud.google.com/vertex-ai/workbench/instances?project={PROJECT_ID}")
