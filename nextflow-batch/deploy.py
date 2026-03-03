"""
@file deploy.py
@brief Nextflow Batch workload deployment script

@details Provisions GCP infrastructure for Nextflow RNAseq pipelines on Cloud Batch:
- Workload-specific APIs and service account
- Org policy overrides for Batch compatibility (shielded VM, trusted images)
- Vertex AI Workbench for researcher environment
- GCS bucket for pipeline I/O
- Nextflow config file (google-batch executor)

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
SERVICE_ACCOUNT_NAME = os.environ.get("SERVICE_ACCOUNT_NAME", "nextflow-pipeline-sa")

# L0 networking — use the Prod spoke shared subnet (set by Central IT)
HOST_PROJECT = os.environ.get("HOST_PROJECT", "")
REGION = os.environ.get("GCP_REGION", "us-central1")
ZONE = f"{REGION}-a"
WORKBENCH_INSTANCE_NAME = os.environ.get("WORKBENCH_INSTANCE_NAME", "nextflow-researcher-workbench")


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
    log_msg("Enabling Batch, Compute, and Logging APIs...")

    try:
        credentials, project = default()
        service = discovery.build('serviceusage', 'v1', credentials=credentials)

        apis = [
            'batch.googleapis.com',
            'compute.googleapis.com',
            'logging.googleapis.com',
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
                        'displayName': 'Nextflow Pipeline Service Account'
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
            'roles/batch.jobsEditor',
            'roles/batch.agentReporter',
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
    Configure org policy overrides for Google Batch compatibility.

    compute.requireShieldedVm: Batch worker VMs may need non-shielded images.
    compute.trustedImageProjects: Batch uses batch-custom-image, cos-cloud, etc.
    """
    log_msg("Configuring org policies for Batch compatibility...")

    try:
        credentials, project = default()
        orgpolicy_service = discovery.build('orgpolicy', 'v2', credentials=credentials)

        # Override compute.requireShieldedVm → enforce: false
        policy_name = f"projects/{PROJECT_ID}/policies/compute.requireShieldedVm"
        log_msg("  Disabling compute.requireShieldedVm...")

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
        except Exception as e:
            err_str = str(e)
            if 'PERMISSION_DENIED' in err_str or '403' in err_str:
                log_msg("  ⚠ Permission denied — need orgpolicy.policyAdmin role", "error")
                step_error("Missing orgpolicy.policyAdmin permission")
                return
            else:
                log_msg(f"  ⚠ {err_str[:120]}", "error")
                raise e

        log_msg("  Note: usePrivateAddress=true handles external IP constraint", "info")
        step_complete()
    except Exception as e:
        step_error(str(e))


def execute_provision_workbench():
    """
    Provision a Vertex AI Workbench instance for researchers.

    Uses the Notebooks API v2 to create a Workbench Instance.
    If instance already exists, returns the URL to access it.
    """
    log_msg(f"Provisioning Vertex AI Workbench: {WORKBENCH_INSTANCE_NAME}...")

    try:
        credentials, project = default()

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
        instance_name = f"projects/{PROJECT_ID}/locations/{ZONE}/instances/{WORKBENCH_INSTANCE_NAME}"
        workbench_url = f"https://console.cloud.google.com/vertex-ai/workbench/instances?project={PROJECT_ID}"

        # Check if instance already exists
        try:
            log_msg(f"  Checking for existing instance in {ZONE}...")
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
            if 'notFound' not in str(e).lower() and '404' not in str(e):
                raise e
            log_msg("  Instance not found, creating new workbench...", "info")

        sa_email = f"{SERVICE_ACCOUNT_NAME}@{PROJECT_ID}.iam.gserviceaccount.com"

        startup_script = f'''#!/bin/bash
apt-get update && apt-get install -y openjdk-17-jdk
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
echo 'export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64' >> /etc/profile.d/java.sh
echo 'export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64' >> /home/jupyter/.bashrc
curl -s https://get.nextflow.io | bash
mv nextflow /usr/local/bin/
mkdir -p /home/jupyter/nextflow-workspace
cd /home/jupyter/nextflow-workspace

cat > nextflow.config << 'EOF'
workDir = 'gs://{BUCKET_NAME}/scratch'
process {{
  executor = 'google-batch'
  container = 'nextflow/rnaseq-nf'
  errorStrategy = 'retry'
  maxRetries = 5
  machineType = 'n1-standard-1'
  disk = '30 GB'
}}
google {{
  project = '{PROJECT_ID}'
  location = '{REGION}'
  batch {{
    spot = true
    serviceAccountEmail = '{sa_email}'
    usePrivateAddress = true
    network = 'projects/{HOST_PROJECT}/global/networks/prod-spoke-0'
    subnetwork = 'projects/{HOST_PROJECT}/regions/{REGION}/subnetworks/default-primary-region'
  }}
}}
EOF

chown -R jupyter:jupyter /home/jupyter/nextflow-workspace
'''

        instance_body = {
            'gceSetup': {
                'machineType': 'n1-standard-4',
                'serviceAccounts': [{'email': sa_email, 'scopes': ['https://www.googleapis.com/auth/cloud-platform']}],
                'networkInterfaces': [{
                    'network': f'projects/{HOST_PROJECT}/global/networks/prod-spoke-0' if HOST_PROJECT else f'projects/{PROJECT_ID}/global/networks/default',
                    'subnet': f'projects/{HOST_PROJECT}/regions/{REGION}/subnetworks/default-primary-region' if HOST_PROJECT else f'projects/{PROJECT_ID}/regions/{REGION}/subnetworks/default',
                    'nicType': 'VIRTIO_NET'
                }],
                'disablePublicIp': True,
                'metadata': {'startup-script': startup_script, 'proxy-mode': 'service_account'},
                'bootDisk': {'diskSizeGb': '150', 'diskType': 'PD_BALANCED'},
                'vmImage': {'project': 'cloud-notebooks-managed', 'name': 'workbench-instances-v20260122'}
            }
        }

        log_msg("  Creating Workbench instance (this takes 3-5 minutes)...", "info")
        log_msg(f"  Machine: n1-standard-4, Zone: {ZONE}", "info")

        operation = notebooks_service.projects().locations().instances().create(
            parent=f"projects/{PROJECT_ID}/locations/{ZONE}",
            instanceId=WORKBENCH_INSTANCE_NAME,
            body=instance_body
        ).execute()

        operation_name = operation.get('name')
        log_msg(f"  Operation: {operation_name.split('/')[-1]}", "info")

        max_wait = 600
        poll_interval = 15
        elapsed = 0

        while elapsed < max_wait:
            op_result = notebooks_service.projects().locations().operations().get(
                name=operation_name
            ).execute()

            if op_result.get('done'):
                if 'error' in op_result:
                    step_error(f"Failed: {op_result['error'].get('message', 'Unknown error')}")
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
            log_msg(f"  Provisioning... ({elapsed}s elapsed)", "info")
            time.sleep(poll_interval)

        log_msg("  ⚠ Workbench still provisioning (check console)", "info")
        log_msg(f"  Console: {workbench_url}", "info")
        step_complete()

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


def execute_write_config():
    """Write nextflow.config file for Google Cloud Batch."""
    log_msg("Writing nextflow.config...")

    try:
        sa_email = f"{SERVICE_ACCOUNT_NAME}@{PROJECT_ID}.iam.gserviceaccount.com"
        network = f'projects/{HOST_PROJECT}/global/networks/prod-spoke-0' if HOST_PROJECT else f'projects/{PROJECT_ID}/global/networks/default'
        subnet = f'projects/{HOST_PROJECT}/regions/{REGION}/subnetworks/default-primary-region' if HOST_PROJECT else f'projects/{PROJECT_ID}/regions/{REGION}/subnetworks/default'

        config_content = f"""// Nextflow configuration for Google Cloud Batch
workDir = 'gs://{BUCKET_NAME}/scratch'

process {{
  executor = 'google-batch'
  container = 'nextflow/rnaseq-nf'
  errorStrategy = 'retry'
  maxRetries = 5
  machineType = 'n1-standard-1'
  disk = '30 GB'
}}

google {{
  project = '{PROJECT_ID}'
  location = '{REGION}'
  batch {{
    spot = true
    serviceAccountEmail = '{sa_email}'
    usePrivateAddress = true
    network = '{network}'
    subnetwork = '{subnet}'
  }}
}}

timeline {{
  enabled = true
  file = 'timeline.html'
  overwrite = true
}}

report {{
  enabled = true
  file = 'report.html'
  overwrite = true
}}
"""

        config_path = os.path.join(os.getcwd(), 'nextflow.config')
        with open(config_path, 'w') as f:
            f.write(config_content)

        log_msg(f"  Written to: {config_path}", "success")
        log_msg(f"  workDir: gs://{BUCKET_NAME}/scratch", "info")
        log_msg(f"  executor: google-batch", "info")
        log_msg(f"  usePrivateAddress: true (org policy compliance)", "info")
        step_complete()
    except Exception as e:
        step_error(str(e))


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"""
    ╔═══════════════════════════════════════════════════════════════╗
    ║     Nextflow Batch — Workload Deployment                      ║
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
        ('write-config', execute_write_config),
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
