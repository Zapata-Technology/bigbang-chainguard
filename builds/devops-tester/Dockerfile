# Big Bang's test-runner image (Iron Bank big-bang/devops-tester) rebuilt FROM the
# org's kubectl or kubectl-fips -dev image. scripts/bb-devops-tester.py passes every
# input from build.yaml, which it generates from Iron Bank's own Dockerfile and
# hardening_manifest.yaml (repo1.dso.mil/dsop/big-bang/devops-tester):
#
#   DEV_IMAGE       kubectl(-fips) -dev image on Iron Bank's kubectl line, pinned by digest
#   VELERO_IMAGE    the org's velero(-fips) image on Iron Bank's velero line, by digest
#   KYVERNO_IMAGE   the org's kyverno-cli(-fips) image on Iron Bank's kyverno line, by digest
#   CHAINSAW_IMAGE  upstream chainsaw at the digest Iron Bank pins (no Chainguard package)
#   PACKAGES        Wolfi packages (bash, curl, mc)
#   USER_NAME, GROUP_NAME, USER_ID   the non-root user Iron Bank creates
ARG DEV_IMAGE
ARG VELERO_IMAGE
ARG KYVERNO_IMAGE
ARG CHAINSAW_IMAGE

FROM ${VELERO_IMAGE} AS velero
FROM ${KYVERNO_IMAGE} AS kyverno
FROM ${CHAINSAW_IMAGE} AS chainsaw

FROM ${DEV_IMAGE}

ARG PACKAGES
ARG USER_NAME=devops-user
ARG GROUP_NAME=devops
ARG USER_ID=1000

USER root

COPY --from=velero /usr/bin/velero /usr/bin/velero
# Iron Bank names the kyverno CLI /usr/bin/kyverno; Chainguard's is kubectl-kyverno.
COPY --from=kyverno /usr/bin/kubectl-kyverno /usr/bin/kyverno
COPY --from=chainsaw /ko-app/chainsaw /usr/bin/chainsaw

RUN set -e; \
    apk add --no-cache ${PACKAGES}; \
    addgroup -g "${USER_ID}" "${GROUP_NAME}"; \
    adduser -D -u "${USER_ID}" -G "${GROUP_NAME}" -s /bin/bash "${USER_NAME}"; \
    chmod 0750 "/home/${USER_NAME}"; \
    mkdir -p "/run/containers/${USER_ID}"; \
    chown -R "${USER_ID}:${USER_ID}" "/run/containers/${USER_ID}"

LABEL org.opencontainers.image.description="Chainguard rebuild of Big Bang's devops-tester (kubectl, velero, kyverno, chainsaw, mc)"

ENTRYPOINT []
CMD ["/bin/bash"]

# Numeric, so pods with runAsNonRoot can verify it.
USER ${USER_ID}:${USER_ID}
