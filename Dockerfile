# syntax=docker/dockerfile:1
# =========================================================================== #
# Hardened & Slimmed Multi-Stage KubeOrbit Enterprise Container Image
# Base: python:3.13-slim-bookworm (Debian 12 Bookworm with latest security patches)
# =========================================================================== #

# --------------------------------------------------------------------------- #
# Stage 1: cli-fetcher -- Download, unpack, and strip cloud/k8s CLI binaries
#          in an isolated stage so archive/build tools never enter runtime.
# --------------------------------------------------------------------------- #
FROM python:3.13-slim-bookworm AS cli-fetcher

ARG KUBECTL_VERSION=v1.32.2
ARG HELM_VERSION=v3.17.1
ARG KUBELOGIN_VERSION=v0.1.7

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates unzip tar binutils \
    && rm -rf /var/lib/apt/lists/*

# 1. kubectl (patched v1.32.2) + strip debug symbols
RUN curl -fsSL --retry 3 -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && (strip --strip-unneeded /usr/local/bin/kubectl || true)

# 2. Helm (patched v3.17.1) + strip debug symbols
RUN curl -fsSL --retry 3 "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" -o /tmp/helm.tgz \
    && tar -xzf /tmp/helm.tgz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && (strip --strip-unneeded /usr/local/bin/helm || true) \
    && rm -rf /tmp/helm.tgz /tmp/linux-amd64

# 3. Azure kubelogin (Entra ID / OIDC AKS tokens) + strip debug symbols
RUN curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors -o /tmp/kubelogin.zip \
        "https://github.com/Azure/kubelogin/releases/latest/download/kubelogin-linux-amd64.zip" \
    && unzip -q /tmp/kubelogin.zip -d /tmp \
    && mv /tmp/bin/linux_amd64/kubelogin /usr/local/bin/kubelogin \
    && chmod +x /usr/local/bin/kubelogin \
    && (strip --strip-unneeded /usr/local/bin/kubelogin || true) \
    && rm -rf /tmp/kubelogin.zip /tmp/bin

# 4. AWS CLI v2 -- prune unused CLI examples & 340+ unused botocore service JSON models
#    (keep eks, sts, iam, ec2, logs, cloudwatch, sso, sso-oidc, s3)
RUN curl -fsSL --retry 3 "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install -i /usr/local/aws-cli -b /usr/local/bin \
    && rm -rf /tmp/awscliv2.zip /tmp/aws \
        /usr/local/aws-cli/v2/current/dist/awscli/examples \
    && find /usr/local/aws-cli/v2/current/dist/awscli/botocore/data -mindepth 1 -maxdepth 1 -type d \
        ! -name 'eks' ! -name 'sts' ! -name 'iam' ! -name 'ec2' \
        ! -name 'logs' ! -name 'cloudwatch' ! -name 'sso*' ! -name 's3' \
        -exec rm -rf {} +


# --------------------------------------------------------------------------- #
# Stage 2: venv-builder -- Build isolated Python virtualenv with upgraded
#          pip/setuptools/wheel and prune unused discovery cache & bytecode.
# --------------------------------------------------------------------------- #
FROM python:3.13-slim-bookworm AS venv-builder

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade "pip>=25.0" "setuptools>=78.1.0" "wheel>=0.45.1" \
    && pip install --no-cache-dir --no-compile -r requirements.txt \
    # Remove 100MB+ static Google API discovery JSON cache & unused bytecode
    && rm -rf /opt/venv/lib/python3.13/site-packages/googleapiclient/discovery_cache/documents \
    && find /opt/venv -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /opt/venv -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete


# --------------------------------------------------------------------------- #
# Stage 3: runtime -- Hardened & Slimmed Debian Bookworm Runtime
# --------------------------------------------------------------------------- #
FROM python:3.13-slim-bookworm AS runtime

# 1. Apply all Debian Bookworm security upgrades & install Google Cloud SDK + Azure CLI,
#    then surgically remove redundant bundled Python, gsutil, bq, and unused CLI modules
#    in the SAME RUN layer so Docker never commits the intermediate bloat.
RUN apt-get update \
    && apt-get dist-upgrade -y \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg apt-transport-https lsb-release \
    # Google Cloud CLI + GKE Auth Plugin
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    # Microsoft Azure CLI apt repository
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bookworm main" \
        > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        google-cloud-cli \
        google-cloud-cli-gke-gcloud-auth-plugin \
        azure-cli \
    # --- Slim down Google Cloud SDK (saves ~630MB) ---
    # Use container's Python 3.13 instead of gcloud's 135MB bundled Python
    && rm -rf /usr/lib/google-cloud-sdk/platform/bundledpythonunix \
              /usr/lib/google-cloud-sdk/platform/gsutil \
              /usr/lib/google-cloud-sdk/platform/bq \
              /usr/lib/google-cloud-sdk/RELEASE_NOTES \
    && find /usr/lib/google-cloud-sdk/lib/googlecloudsdk/generated_clients/apis -mindepth 1 -maxdepth 1 -type d \
        ! -name 'container*' ! -name 'iam*' ! -name 'cloudresourcemanager*' \
        ! -name 'compute*' ! -name 'logging*' ! -name 'storage*' ! -name 'serviceusage*' ! -name 'sts*' \
        -exec rm -rf {} + \
    && find /usr/lib/google-cloud-sdk -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/lib/google-cloud-sdk -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete \
    # --- Slim down Azure CLI (saves ~430MB) ---
    # Keep acs (az aks), profile (az login/account), role, resource, monitor, cloud, configure, core
    && rm -rf /opt/az/lib/python*/site-packages/pip \
              /opt/az/lib/python*/site-packages/azure/synapse \
              /opt/az/lib/python*/site-packages/azure/batch \
              /opt/az/lib/python*/site-packages/azure/cosmos \
              /opt/az/lib/python*/site-packages/azure/datalake \
    && find /opt/az/lib/python*/site-packages/azure/cli/command_modules -mindepth 1 -maxdepth 1 -type d \
        ! -name 'acs' ! -name 'profile' ! -name 'resource' ! -name 'role' \
        ! -name 'monitor' ! -name 'cloud' ! -name 'configure' ! -name 'util' ! -name ' interactive' \
        -exec rm -rf {} + \
    && find /opt/az/lib/python*/site-packages/azure/mgmt -mindepth 1 -maxdepth 1 -type d \
        ! -name 'containerservice*' ! -name 'core*' ! -name 'resource*' \
        ! -name 'authorization*' ! -name 'monitor*' ! -name 'msi*' ! -name 'compute*' ! -name 'network*' \
        -exec rm -rf {} + \
    && find /opt/az -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) -exec rm -rf {} + 2>/dev/null || true \
    && find /opt/az -type f \( -name "*.pyc" -o -name "*.pyo" -o -name "*.a" \) -delete \
    # Purge build-only apt packages & apt caches
    && apt-get purge -y --auto-remove gnupg lsb-release \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /usr/share/doc/* /usr/share/man/*

# 2. Remediate base-image Python packaging CVEs in system site-packages
RUN pip install --no-cache-dir --no-compile --upgrade "pip>=25.0" "setuptools>=78.1.0" "wheel>=0.45.1" \
    && find /usr/local/lib/python3.13 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# 3. Copy pre-fetched & stripped CLI binaries from Stage 1
COPY --from=cli-fetcher /usr/local/bin/kubectl /usr/local/bin/kubectl
COPY --from=cli-fetcher /usr/local/bin/helm /usr/local/bin/helm
COPY --from=cli-fetcher /usr/local/bin/kubelogin /usr/local/bin/kubelogin
COPY --from=cli-fetcher /usr/local/aws-cli /usr/local/aws-cli
RUN ln -sf /usr/local/aws-cli/v2/current/bin/aws /usr/local/bin/aws \
    && ln -sf /usr/local/aws-cli/v2/current/bin/aws_completer /usr/local/bin/aws_completer

# 4. Copy isolated application virtualenv from Stage 2
COPY --from=venv-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    CLOUDSDK_PYTHON="/usr/local/bin/python3" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 5. Copy KubeOrbit application source code & safe fallback config
WORKDIR /app
COPY app.py auth.py config.py ai_agent.py ./
COPY kube_client ./kube_client
COPY providers ./providers
COPY mcp_server ./mcp_server
COPY static ./static
COPY config/clusters.example.yaml ./config/clusters.example.yaml

# 6. Create non-root runtime user (UID 10001) and writable config/kubeconfig directories
RUN useradd --create-home --uid 10001 --shell /bin/sh kubeorbit \
    && mkdir -p /home/kubeorbit/.kubeorbit/kubeconfigs /app/config \
    && cp /app/config/clusters.example.yaml /app/config/clusters.yaml \
    && chown -R kubeorbit:kubeorbit /home/kubeorbit /app

USER kubeorbit

ENV KUBEORBIT_KUBECONFIG_HOME=/home/kubeorbit/.kubeorbit/kubeconfigs \
    KUBEORBIT_CONFIG_PATH=/app/config/clusters.yaml \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8080}/api/health || exit 1

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
