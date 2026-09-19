# KubeOrbit Local Docker Runner (Zero-Keys Multi-Cloud Session Mount)
# Run from repository root

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Starting KubeOrbit in Docker Container" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# 1. Ensure placeholder credential folders exist on host
$placeholders = @("$env:USERPROFILE\.aws", "$env:USERPROFILE\.azure")
foreach ($p in $placeholders) {
    if (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Force -Path $p | Out-Null
    }
}

# 2. Build or pull the image
Write-Host "`n[1/3] Building KubeOrbit image (kubeorbit:latest)..." -ForegroundColor Yellow
docker build -t kubeorbit:latest .

# 3. Stop existing container if running
Write-Host "`n[2/3] Checking for running containers..." -ForegroundColor Yellow
docker stop kubeorbit 2>$null
docker rm kubeorbit 2>$null

# 4. Start the container with mounted host cloud sessions
Write-Host "`n[3/3] Starting container with mounted cloud sessions..." -ForegroundColor Yellow
docker run -d `
  -p 8080:8080 `
  -v "${env:APPDATA}\gcloud:/home/kubeorbit/.config/gcloud:ro" `
  -v "${env:USERPROFILE}\.aws:/home/kubeorbit/.aws:ro" `
  -v "${env:USERPROFILE}\.azure:/home/kubeorbit/.azure:ro" `
  -v "${env:USERPROFILE}\.kube:/home/kubeorbit/.kube:ro" `
  -v "${PWD}\config\clusters.yaml:/app/config/clusters.yaml:ro" `
  --name kubeorbit `
  kubeorbit:latest

Write-Host "`n--------------------------------------------------" -ForegroundColor Green
Write-Host "  KubeOrbit is running on: http://127.0.0.1:8080" -ForegroundColor Green
Write-Host "  - GCP Auth: Mounted from active gcloud login" -ForegroundColor Green
Write-Host "  - AWS / Azure: Placeholders mounted for future" -ForegroundColor Green
Write-Host "  - Local K8s: Mounted from host ~/.kube" -ForegroundColor Green
Write-Host "--------------------------------------------------" -ForegroundColor Green
