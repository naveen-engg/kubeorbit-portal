# KubeOrbit container image.
#
# Installs kubectl + helm plus all three cloud CLIs (gcloud, aws, az +
# kubelogin) so one image can be deployed to any provider's managed runtime
# (Cloud Run, Container Apps/App Service) and reach GKE/EKS/AKS clusters
# alike. This makes the image large (~1.5GB); if you only ever run against
# one cloud, delete the other two CLI installation stages below to shrink it.
FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.31.0
ARG HELM_VERSION=v3.16.3

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg unzip lsb-release \
    && rm -rf /var/lib/apt/lists/*

# kubectl -- every provider adapter hands off to this
RUN curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# Helm (Cluster Overview's Helm release listing)
RUN curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" -o /tmp/helm.tgz \
    && tar -xzf /tmp/helm.tgz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/helm.tgz /tmp/linux-amd64

# gcloud CLI & GKE Auth Plugin -- providers/gke.py
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        google-cloud-cli \
        google-cloud-cli-gke-gcloud-auth-plugin \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 -- providers/eks.py
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

# Azure CLI + kubelogin -- providers/aks.py (kubelogin is required for
# Azure AD/Entra RBAC-enabled clusters to mint kubectl tokens)
RUN curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash \
    && curl -fsSL -o /tmp/kubelogin.zip \
        "https://github.com/Azure/kubelogin/releases/latest/download/kubelogin-linux-amd64.zip" \
    && unzip -q /tmp/kubelogin.zip -d /tmp \
    && mv /tmp/bin/linux_amd64/kubelogin /usr/local/bin/kubelogin \
    && rm -rf /tmp/kubelogin.zip /tmp/bin

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py auth.py config.py ai_agent.py ./
COPY kube_client ./kube_client
COPY providers ./providers
COPY mcp_server ./mcp_server
COPY static ./static

# config/clusters.yaml is deployment-specific and intentionally NOT baked in
# here -- mount it (Cloud Run Secret Manager volume, Container Apps/App
# Service volume, or a custom build step) and point KUBEORBIT_CONFIG_PATH at
# it. See README.md "Deployment" sections.

RUN useradd --create-home --uid 10001 kubeorbit \
    && mkdir -p /home/kubeorbit/.kubeorbit/kubeconfigs \
    && chown -R kubeorbit:kubeorbit /home/kubeorbit /app
USER kubeorbit

ENV KUBEORBIT_KUBECONFIG_HOME=/home/kubeorbit/.kubeorbit/kubeconfigs \
    KUBEORBIT_CONFIG_PATH=/app/config/clusters.yaml \
    PORT=8080

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
