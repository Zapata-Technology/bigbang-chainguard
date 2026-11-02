#!/bin/sh
# Admission, with nothing created (server dry-run): a Pod that meets Big Bang's Enforce
# policies is admitted on an org image (the running admission controller's) and refused,
# by restrict-image-registries, with the same spec on a registry the overlay doesn't allow.
#   sh compat/support/kyverno-admission.sh <namespace to dry-run the Pods in>
set -e
ns=$1
img=$(kubectl -n kyverno get pod -l app.kubernetes.io/component=admission-controller -o jsonpath='{.items[0].spec.containers[0].image}')
spec() { printf '{"spec":{"automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"runAsUser":65532,"runAsGroup":65532,"fsGroup":65532,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"probe","image":"%s","securityContext":{"allowPrivilegeEscalation":false,"privileged":false,"readOnlyRootFilesystem":true,"runAsNonRoot":true,"capabilities":{"drop":["ALL"]}},"resources":{"limits":{"cpu":"50m","memory":"32Mi"},"requests":{"cpu":"50m","memory":"32Mi"}}}]}}' "$1"; }
kubectl -n "$ns" run kyverno-check-org --image="$img" --restart=Never --dry-run=server --overrides="$(spec "$img")" -o name
echo "admitted $img"
bad=docker.io/library/busybox:1.36
if kubectl -n "$ns" run kyverno-check-bad --image=$bad --restart=Never --dry-run=server --overrides="$(spec $bad)" -o name 2>"$COMPAT_OUT/kyverno-refused.txt"; then
  echo "admitted $bad"; exit 1
fi
cat "$COMPAT_OUT/kyverno-refused.txt"
grep -q restrict-image-registries "$COMPAT_OUT/kyverno-refused.txt"
