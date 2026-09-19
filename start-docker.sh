#!/usr/bin/env bash
set -e

echo "========================================="
echo "  Starting KubeOrbit in Docker Container"
echo "========================================="

# 1. Ensure placeholder credential folders exist on host
mkdir -p "$HOME/.aws" "$HOME/.azure" "$HOME/.kube" "$HOME/.config/gcloud"

# 2. Build the image
echo -e "\n[1/3] Building KubeOrbit image (kubeorbit:latest)..."
docker build -t kubeorbit:latest .

# 3. Stop existing container if running
echo -e "\n[2/3] Checking for running containers..."
docker stop kubeorbit 2>/dev/null || true
docker rm kubeorbit 2>/dev/null || true

# 4. Start the container with mounted host cloud sessions
echo -e "\n[3/3] Starting container with mounted cloud sessions..."
docker run -d \
  -p 8080:8080 \
  -v "$HOME/.config/gcloud:/home/kubeorbit/.config/gcloud:ro" \
  -v "$HOME/.aws:/home/kubeorbit/.aws:ro" \
  -v "$HOME/.azure:/home/kubeorbit/.azure:ro" \
  -v "$HOME/.kube:/home/kubeorbit/.kube:ro" \
  -v "$(pwd)/config/clusters.yaml:/app/config/clusters.yaml:ro" \
  --name kubeorbit \
  kubeorbit:latest

echo -e "\n--------------------------------------------------"
echo "  KubeOrbit is running on: http://127.0.0.1:8080"
echo "  - GCP Auth: Mounted from active gcloud login"
echo "  - AWS / Azure: Placeholders mounted for future"
echo "  - Local K8s: Mounted from host ~/.kube"
echo "--------------------------------------------------"
