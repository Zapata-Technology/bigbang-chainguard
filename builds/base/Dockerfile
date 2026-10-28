# Big Bang's job base image (Iron Bank big-bang/base) rebuilt FROM the org's
# kubectl or kubectl-fips -dev image. Big Bang's jobs run bash scripts, so the
# image needs a shell and the -dev image is the runtime. scripts/bb-base.py passes
# every input from build.yaml, which it generates from Iron Bank's own
# hardening_manifest.yaml and Dockerfile (repo1.dso.mil/dsop/big-bang/base):
#
#   DEV_IMAGE   kubectl(-fips) -dev image, pinned by digest
#   PACKAGES    Wolfi packages for Iron Bank's microdnf installs and UBI userland
#   USER_NAME, USER_ID   the non-root user Iron Bank creates
ARG DEV_IMAGE

FROM ${DEV_IMAGE}

ARG PACKAGES
ARG USER_NAME=base
ARG USER_ID=1000

USER root

RUN set -e; \
    apk add --no-cache ${PACKAGES}; \
    addgroup -g "${USER_ID}" "${USER_NAME}"; \
    adduser -D -u "${USER_ID}" -G "${USER_NAME}" -s /sbin/nologin "${USER_NAME}"

LABEL org.opencontainers.image.description="Chainguard rebuild of Big Bang's base job image (bash, kubectl, yq, jq, curl)"

# Iron Bank's image is UBI with no entrypoint; Big Bang's jobs pass their own command.
ENTRYPOINT []
CMD ["/bin/bash"]

# Numeric, so pods with runAsNonRoot can verify it.
USER ${USER_ID}:${USER_ID}
