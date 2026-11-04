# Chainguard Vault plus curl, for Big Bang's vault package. scripts/bb-build.py vault passes
# every input from build.yaml.
#
# Big Bang's vault chart runs a post-start hook (templates/bigbang/vault-post-start.yaml)
# that polls http://127.0.0.1:8200/v1/sys/seal-status with curl and calls the
# Kubernetes API with it. Chainguard's vault image has a shell and busybox but no
# curl, so the hook times out and Vault is killed. Iron Bank's Vault (UBI) has curl.
#
#   DEV_IMAGE   the org's vault(-fips) -dev image, pinned by digest
#   PACKAGES    Wolfi packages to add (curl)
#   RUN_USER    the user the vault image runs as (read from the image by bb-build.py vault)
ARG DEV_IMAGE

FROM ${DEV_IMAGE}

ARG PACKAGES
ARG RUN_USER

USER root

RUN apk add --no-cache ${PACKAGES}

LABEL org.opencontainers.image.description="Chainguard Vault with curl for Big Bang's vault post-start hook"

# Entrypoint and command are the vault image's own.
USER ${RUN_USER}
