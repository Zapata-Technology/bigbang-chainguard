# Big Bang's test-runner image (Iron Bank big-bang/devops-tester) rebuilt FROM the
# org's kubectl or kubectl-fips -dev image. scripts/bb-build.py devops-tester passes every
# input from build.yaml, which it generates from Iron Bank's own Dockerfile and
# hardening_manifest.yaml (repo1.dso.mil/dsop/big-bang/devops-tester):
#
#   DEV_IMAGE       kubectl(-fips) -dev image on Iron Bank's kubectl line, pinned by digest
#   VELERO_IMAGE    the org's velero(-fips) image on Iron Bank's velero line, by digest
#   KYVERNO_IMAGE   the org's kyverno-cli(-fips) image on Iron Bank's kyverno line, by digest
#   CHAINSAW_*      chainsaw's source tarball, its sha256, version and the modules to raise
#                   (no Chainguard package; built here with Wolfi's Go)
#   PACKAGES        Wolfi packages (bash, curl, mc)
#   USER_NAME, GROUP_NAME, USER_ID   the non-root user Iron Bank creates
ARG DEV_IMAGE
ARG VELERO_IMAGE
ARG KYVERNO_IMAGE

FROM ${VELERO_IMAGE} AS velero
FROM ${KYVERNO_IMAGE} AS kyverno

FROM ${DEV_IMAGE} AS chainsaw

ARG CHAINSAW_SOURCE
ARG CHAINSAW_SHA256
ARG CHAINSAW_VERSION
ARG CHAINSAW_GO_GET=""

USER root

# Upstream's Makefile build, with the current Go and CHAINSAW_GO_GET's modules raised.
RUN set -e;     apk add --no-cache go curl;     curl -fsSL -o /tmp/src.tgz "${CHAINSAW_SOURCE}";     echo "${CHAINSAW_SHA256}  /tmp/src.tgz" | sha256sum -c -;     mkdir -p /src /out;     tar -xzf /tmp/src.tgz -C /src --strip-components=1;     cd /src;     export GOTOOLCHAIN=local CGO_ENABLED=0 GOFLAGS=-mod=mod;     if [ -n "${CHAINSAW_GO_GET}" ]; then go get ${CHAINSAW_GO_GET}; fi;     go build -trimpath -o /out/chainsaw       -ldflags "-s -w -X github.com/kyverno/chainsaw/pkg/version.BuildVersion=v${CHAINSAW_VERSION}" .

FROM ${DEV_IMAGE}

ARG PACKAGES
ARG USER_NAME=devops-user
ARG GROUP_NAME=devops
ARG USER_ID=1000

USER root

COPY --from=velero /usr/bin/velero /usr/bin/velero
# Iron Bank names the kyverno CLI /usr/bin/kyverno; Chainguard's is kubectl-kyverno.
COPY --from=kyverno /usr/bin/kubectl-kyverno /usr/bin/kyverno
COPY --from=chainsaw /out/chainsaw /usr/bin/chainsaw

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
