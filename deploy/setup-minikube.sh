#!/usr/bin/env bash
# One-time (or after `minikube delete`) setup for the local minikube instance
# that dev + staging share (docs/dev-stage-minikube-runbook.md). Brings up the cluster
# with etcd encryption-at-rest and the shared Postgres.
#
# Two things aren't the plain `minikube start` defaults, both required:
#
# 1. --container-runtime=containerd — the default cri-o crashes on this
#    podman/macOS combo with "error setting rlimit type 7: operation not
#    permitted" (a rootless-podman/runc interaction, unrelated to this repo).
#
# 2. etcd encryption-at-rest — plain k8s Secrets are only base64 in etcd, not
#    encrypted. Getting a --encryption-provider-config file onto the node
#    needs care: minikube's apiserver static pod does NOT reliably pick up a
#    *newly added* hostPath volume (confirmed: manual manifest edits and
#    kubeadm-native --extra-config both failed with "no such file or
#    directory" even when the file was verified present and the volumeMount
#    correctly declared — a known, general minikube limitation, not specific
#    to this driver: https://github.com/kubernetes/minikube/issues/9339).
#    The fix is to reuse /var/lib/minikube/certs/, which the apiserver static
#    pod already mounts (that's how it gets its TLS certs) — so the
#    encryption config just needs to land in that directory before kubeadm's
#    control-plane-check times out (~4 min), which this script races for
#    right after the node's SSH comes up (kubeadm takes longer than that to
#    even start the apiserver).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENCRYPTION_CONFIG_PATH=/var/lib/minikube/certs/encryption-config.yaml

echo "==> Starting minikube (background, containerd + encryption-provider-config)"
minikube start --driver=podman --container-runtime=containerd \
  --extra-config="apiserver.encryption-provider-config=${ENCRYPTION_CONFIG_PATH}" \
  > /tmp/minikube-start.log 2>&1 &
START_PID=$!

echo "==> Waiting for node SSH, then placing the encryption config"
for i in $(seq 1 60); do
  if minikube ssh -- "echo ready" 2>/dev/null | grep -q ready; then
    break
  fi
  sleep 2
done

KEY=$(head -c 32 /dev/urandom | base64)
TMP_CONFIG=$(mktemp)
cat > "$TMP_CONFIG" <<EOF
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources:
      - secrets
    providers:
      - aescbc:
          keys:
            - name: key1
              secret: ${KEY}
      - identity: {}
EOF

minikube cp "$TMP_CONFIG" /tmp/encryption-config.yaml
rm -f "$TMP_CONFIG"

for i in $(seq 1 30); do
  if minikube ssh -- "sudo test -d /var/lib/minikube/certs && sudo mv /tmp/encryption-config.yaml ${ENCRYPTION_CONFIG_PATH} && sudo chmod 644 ${ENCRYPTION_CONFIG_PATH} && echo PLACED" 2>/dev/null | grep -q PLACED; then
    echo "==> Encryption config placed"
    break
  fi
  sleep 1
done

echo "==> Waiting for minikube start to finish"
wait "$START_PID"
cat /tmp/minikube-start.log

echo "==> Verifying node is Ready and encryption is active"
kubectl wait --for=condition=Ready node/minikube --timeout=120s
minikube ssh -- "ps aux | grep '[k]ube-apiserver' | grep -o 'encryption-provider-config=[^ ]*'"

echo "==> Applying shared Postgres (data namespace)"
# Shared multi-app Postgres lives in a neutral `data` namespace, not any one
# app's namespace (docs/PLAN-postgres-role-isolation.md). The manifest creates
# the namespace itself; the Secret is created here (never committed).
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-dev}"   # superuser bootstrap only
APP_PASSWORD="${APP_PASSWORD:-dev}"             # snoopy's app-role login
kubectl apply -f "$REPO_DIR/deploy/k8s/postgres.yaml"
kubectl create secret generic postgres-secret -n data \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n data rollout status deploy/postgres --timeout=120s

# Per-app least-privilege role owning only its own database, confined via
# REVOKE CONNECT ... FROM PUBLIC (idempotent — safe to re-run).
echo "==> Provisioning snoopy_rw role + confinement on snoopy_home"
kubectl -n data exec -i deploy/postgres -- psql -U postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='snoopy_rw') THEN
    CREATE ROLE snoopy_rw LOGIN PASSWORD '${APP_PASSWORD}';
  END IF;
END \$\$;
ALTER DATABASE snoopy_home OWNER TO snoopy_rw;
REVOKE CONNECT ON DATABASE snoopy_home FROM PUBLIC;
GRANT  CONNECT ON DATABASE snoopy_home TO snoopy_rw;
SQL
kubectl -n data exec -i deploy/postgres -- psql -U postgres -d snoopy_home -c \
  "ALTER SCHEMA public OWNER TO snoopy_rw;"

echo "==> Done. Reach Postgres locally with:"
echo "    kubectl -n data port-forward svc/postgres 5432:5432 &"
echo "    then connect as: postgresql://snoopy_rw:${APP_PASSWORD}@localhost:5432/snoopy_home"
