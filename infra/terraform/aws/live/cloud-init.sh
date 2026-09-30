#!/bin/bash
# cloud-init for the demo VM: Docker + compose datastores, k3s, Helm chart, Traefik TLS.
# Rendered by Terraform with: hostslip, lake, anchors, kms_key.
set -euo pipefail

HOSTSLIP="${hostslip}"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq docker.io docker-compose-plugin curl

# --- datastores (same compose file as local) ---
git clone --depth 1 <repo-url> /opt/fleetsentinel 2>/dev/null || true
cd /opt/fleetsentinel/deploy/compose
export S3_BUCKET="${lake}" ANCHOR_BUCKET="${anchors}" KMS_KEY="${kms_key}"
docker compose --profile infra up -d postgres redis mongo minio redpanda mosquitto keycloak vault

# --- k3s + Helm chart (the SAME chart as kind) ---
curl -sfL https://get.k3s.io | sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
curl -sSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
sed -e "s/__HOSTSLIP__/$HOSTSLIP/g" \
    /opt/fleetsentinel/deploy/helm/fleetsentinel/values-vm.yaml > /tmp/values-vm.yaml
# __HOSTIP__ = primary private IP
sed -i "s/__HOSTIP__/$(hostname -I | awk '{print $1}')/g" /tmp/values-vm.yaml
helm install fs /opt/fleetsentinel/deploy/helm/fleetsentinel -f /tmp/values-vm.yaml

echo "demo ready: https://app.$HOSTSLIP.sslip.io"
