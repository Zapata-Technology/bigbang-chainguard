#!/bin/sh
# Kyverno's signature policy in Enforce (a compat `check` command): a signed org image
# is admitted, an org image with nothing to verify is refused. Admission is enough: the
# Pods never need to start.
set -e
img=$(kubectl -n kyverno get pod -o jsonpath='{.items[0].spec.containers[0].image}')
repo=${img%%[:@]*}
kubectl -n default run sig-signed --image="$img" --restart=Never --command -- true
echo "admitted $img"
kubectl -n default delete pod sig-signed --wait=false
if kubectl -n default run sig-unsigned --image="$repo:no-such-tag-0" --restart=Never --command -- true 2>"$COMPAT_OUT/err.txt"; then
  echo "admitted $repo:no-such-tag-0, which has no signature"; exit 1
fi
echo "refused $repo:no-such-tag-0: $(tail -c 300 "$COMPAT_OUT/err.txt")"
