#!/bin/sh
# Kyverno's signature policy in Enforce (a compat `check` command): a signed org image is
# admitted, an org image with nothing to verify is refused, and the same for an image this
# repo builds (builds.yml's signature). Admission is enough: the Pods never need to start.
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

# The images this repo builds: builds.yml's signature (the policy's second rule).
suffix=; [ -n "$COMPAT_FIPS" ] && suffix=-fips
dir="$COMPAT_OUT/overlays"; [ -d "$dir" ] || dir=overlays
built=$(grep -ohE "[a-z0-9.-]+\.[a-z]{2,}/[a-z0-9._-]+/bigbang-base$suffix:[A-Za-z0-9._-]+" "$dir"/*.yaml | head -1)
if [ -n "$built" ]; then
  brepo=${built%:*}
  kubectl -n default run sig-built --image="$built" --restart=Never --command -- true
  echo "admitted $built"
  kubectl -n default delete pod sig-built --wait=false
  if kubectl -n default run sig-built-unsigned --image="$brepo:no-such-tag-0" --restart=Never --command -- true 2>"$COMPAT_OUT/err.txt"; then
    echo "admitted $brepo:no-such-tag-0, which has no signature"; exit 1
  fi
  echo "refused $brepo:no-such-tag-0: $(tail -c 300 "$COMPAT_OUT/err.txt")"
fi
