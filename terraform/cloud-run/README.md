# KubeOrbit on GCP Cloud Run with Identity-Aware Proxy (IAP)

This Terraform module deploys **KubeOrbit** to **Google Cloud Run (v2)** with:
- **Zero-Key Multi-Cloud Identity (`kubeorbit-runner`)**: Dedicated Google Service Account whose OIDC ID tokens authenticate to **GKE** (via IAM Impersonation), **AWS EKS** (via `sts:AssumeRoleWithWebIdentity`), and **Azure AKS** (via Entra ID Federated Identity Credential).
- **Direct VPC Egress + Cloud NAT (Static IP)**: Routes private RFC 1918 traffic over your VPC and internet-bound traffic through a deterministic static IP (`nat_static_ip`) that you can allowlist on GKE, EKS, and AKS control planes.
- **Secret Manager Seed + Writable Runtime Config**: Seeds `/etc/kubeorbit-seed/clusters.yaml` from Secret Manager and copies it to `/tmp/clusters.yaml` at container startup so in-UI Cluster Registration & Edit operations work seamlessly.
- **Enterprise Security Hardening**: Sets `KUBEORBIT_AUTH_MODE=iap`, `KUBEORBIT_DISABLE_LOCAL_PROVIDER=true`, `KUBEORBIT_ADMIN_EMAILS`, and `KUBEORBIT_OPERATOR_EMAILS`.

---

## Step-by-Step Deployment

### Step 1: Build & Push the KubeOrbit Image
From the repository root (`c:\Users\navee\Kube-orbit`):

```bash
export PROJECT_ID="adk-enterprise-observability"
export REGION="us-central1"

# Create Artifact Registry or let Terraform create it first via targeted apply:
cd terraform/cloud-run
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform apply -target=google_artifact_registry_repository.kubeorbit

# Build and push the multi-cloud container image (includes kubectl, helm, gcloud, aws, az, kubelogin)
cd ../..
gcloud builds submit \
  --tag "${REGION}-docker.pkg.dev/${PROJECT_ID}/kubeorbit-repo/kubeorbit:enterprise" \
  --project="${PROJECT_ID}"
```

### Step 2: Apply the Full Cloud Run + IAP Stack
```bash
cd terraform/cloud-run
terraform apply
```

### Step 3: Capture Outputs for GKE, EKS, and AKS Modules
After `terraform apply` completes, record the three key outputs:

```bash
terraform output
```
- **`runner_sa_email`**: Pass this as `cloudrun_sa_email` to `terraform/gke`.
- **`runner_sa_unique_id`**: Pass this numeric OAuth2 Subject ID (`sub`) as `cloudrun_sa_unique_id` to `terraform/eks` and `terraform/aks`.
- **`nat_static_ip`**: Pass this IP to `terraform/gke`, `terraform/eks`, and `terraform/aks` so their Kubernetes API server firewalls allowlist KubeOrbit's outbound IP.

### Step 4: Enable Native Cloud Run IAP Flag (if not using Load Balancer IAP)
```bash
gcloud beta run services update kubeorbit \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --iap
```
