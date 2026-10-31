#!/usr/bin/env bash
# Run bb-install.py from the installer image, so the workstation needs only docker and a
# kubectl context: no kubectl, helm or Python.
#
#   scripts/bb-docker.sh install [--fips] [...]    any bb-install.py command and flags
#   BB_INSTALLER_FIPS=1 scripts/bb-docker.sh ...   the -fips installer image (a FIPS workstation)
#
# The image is <build registry>/bigbang-installer[-fips]:<tag>, the tag from
# builds/installer/build.yaml and the registry from the overlays, so it matches this
# checkout. The current directory is mounted at /site and is the working directory there,
# so `--site my-site.yaml` (the default) is the file here. It runs as your uid, so it can
# read your kubeconfig and the files it writes are yours. ~/.kube (or $KUBE_DIR) is mounted
# read-only; a kubeconfig that runs a login plugin (aws, az, gke-gcloud-auth-plugin) or
# points at certificate files outside it needs a self-contained context instead (`kubectl
# config view --minify --flatten`), since the container has neither.
#
# Credentials pass through from the environment when set (never on the command line):
# CGR_*, BUILD_REGISTRY_*, REGISTRY1_*, MIRROR_*, S3_*, CHAINGUARD_ORG_ID. Without
# chainctl in the image, a first install needs CGR_USERNAME and CGR_PASSWORD (a pull
# token); later runs read it from the cluster. Pulling the image itself needs
# `docker login <build registry>` when the image is private.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
variant=standard name=bigbang-installer
if [ "${BB_INSTALLER_FIPS:-}" = 1 ]; then variant=fips name=bigbang-installer-fips; fi

# image: <variant>: ... tag: <tag> in the generated build.yaml.
tag="$(awk -v v="  $variant:" '$0 == v {f=1; next} f && $1 == "tag:" {print $2; exit}' \
  "$root/builds/installer/build.yaml")"
registry="$(grep -oE '[a-z0-9.-]+\.[a-z]{2,}/[A-Za-z0-9._-]+/bigbang-' "$root/overlays/values-chainguard.yaml" \
  | grep -v '^cgr\.dev/' | head -1 | sed 's#/bigbang-$##' || true)"
registry="${BUILD_REGISTRY:-$registry}"
if [ -z "$tag" ] || [ -z "$registry" ]; then
  echo "bb-docker.sh: no installer image here (builds/installer/build.yaml, or no BUILD_REGISTRY in the overlays)" >&2
  exit 1
fi

envs=()
for v in CGR_USERNAME CGR_PASSWORD BUILD_REGISTRY_USERNAME BUILD_REGISTRY_PASSWORD \
         REGISTRY1_USERNAME REGISTRY1_PASSWORD MIRROR_USERNAME MIRROR_PASSWORD \
         S3_ACCESS_KEY S3_SECRET_KEY CHAINGUARD_ORG_ID; do
  if [ -n "${!v:-}" ]; then envs+=(-e "$v"); fi       # the name only: docker reads the value
done
tty=(-i)
if [ -t 0 ] && [ -t 1 ]; then tty=(-it); fi            # prompts when run by hand, not in a pipe

# MSYS_NO_PATHCONV: Git Bash on Windows would otherwise rewrite the container paths.
# HOME=/tmp: the image's home belongs to its own user; kubectl and helm keep caches there.
exec env MSYS_NO_PATHCONV=1 docker run --rm "${tty[@]}" --network host \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e KUBECONFIG=/kube/config \
  -v "${KUBE_DIR:-$HOME/.kube}:/kube:ro" -v "$PWD:/site" -w /site \
  ${envs[@]+"${envs[@]}"} "$registry/$name:$tag" "$@"
