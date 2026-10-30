#!/bin/sh
# HTTPS through Big Bang's public gateway, verified with the CA bb-install.py made
# (a compat `check` command):  sh compat/support/https-check.sh <host under the domain>
set -e
host=$1
kubectl -n istio-gateway wait certificate/public-cert --for=condition=Ready --timeout=300s
kubectl -n cert-manager get secret bb-install-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > "$COMPAT_OUT/ca.crt"
svc=$(kubectl -n istio-gateway get svc -o name | grep public | head -1)
kubectl -n istio-gateway port-forward "$svc" 18443:443 >/dev/null 2>&1 &
pf=$!
trap 'kill $pf' EXIT
sleep 5
# curl fails (exit 60) unless the gateway's certificate chains to the installer's CA and
# names $host; any HTTP status after the handshake (404: no app there) is a pass.
code=$(curl -sS --retry 5 --retry-all-errors --retry-delay 3 --cacert "$COMPAT_OUT/ca.crt" \
  --resolve "$host:18443:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$host:18443/")
echo "https://$host -> HTTP $code; certificate verified against the installer's CA"
