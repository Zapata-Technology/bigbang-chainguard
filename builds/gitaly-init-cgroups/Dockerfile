# GitLab's gitaly-init-cgroups (registry1 gitlab/gitlab-org/build/cng/gitaly-init-cgroups)
# built from Gitaly's source FROM the org's images, as GitLab's CNG builds it (CNG's
# gitaly/Dockerfile builds tools/gitaly-init-cgroups; gitaly-init-cgroups/Dockerfile copies
# it onto scratch). Chainguard publishes no such image; Iron Bank has no recipe.
# scripts/bb-gitaly-init-cgroups.py passes every input from build.yaml:
#
#   DEV_IMAGE          the org's build image (go(-fips) -dev, or kubectl(-fips) -dev), by digest
#   BASE_IMAGE         the org's runtime image (glibc-openssl(-fips), or kubectl(-fips)), by digest
#   GO_PACKAGE         Wolfi Go package to add when DEV_IMAGE has no Go ("" otherwise)
#   GITALY_*           GitLab's archive of the module's directory at the Gitaly tag, its
#                      sha256, the module directory, any modules to raise, and Gitaly's
#                      LICENSE (MIT; outside the module's directory) with its sha256
#   FIPS               "toolchain" (the build image's FIPS Go), "native" (Go's own FIPS
#                      140-3 module), or "" (standard)
#   BINARY, RUN_USER   CNG's binary path and command (/setup_cgroups), and root (CNG's user)
ARG DEV_IMAGE
ARG BASE_IMAGE

FROM ${DEV_IMAGE} AS build

ARG GO_PACKAGE=""
ARG GITALY_SOURCE
ARG GITALY_SHA256
ARG GITALY_MODULE
ARG GITALY_GO_GET=""
ARG GITALY_LICENSE
ARG GITALY_LICENSE_SHA256
ARG FIPS=""

USER root

# CNG's build: CGO_ENABLED=0 go build -trimpath in the module's directory.
# FIPS=toolchain: the build image's Go is Chainguard's FIPS Go (go-fips): cgo.
# FIPS=native: no FIPS toolchain in the org (kubectl-fips -dev offers only go-1.N): build
#   against Go's own FIPS 140-3 module, as builds/mattermost-operator/Dockerfile explains.
RUN set -e; \
    command -v go >/dev/null || apk add --no-cache ${GO_PACKAGE}; \
    command -v curl >/dev/null || apk add --no-cache curl; \
    if [ "${FIPS}" = toolchain ]; then command -v gcc >/dev/null || apk add --no-cache gcc glibc-dev; fi; \
    curl -fsSL -o /tmp/src.tgz "${GITALY_SOURCE}"; \
    echo "${GITALY_SHA256}  /tmp/src.tgz" | sha256sum -c -; \
    mkdir -p /src /out; \
    tar -xzf /tmp/src.tgz -C /src --strip-components=1; \
    cd "/src/${GITALY_MODULE}"; \
    export GOTOOLCHAIN=local GOFLAGS=-mod=mod CGO_ENABLED=0; \
    case "${FIPS}" in \
      toolchain) export CGO_ENABLED=1;; \
      native) dir="$(go env GOROOT)/lib/fips140"; \
        line=$(grep -E '^v1\.0\.0-[0-9a-f]+\.zip ' "$dir/fips140.sum" | tail -1); \
        [ -n "$line" ] || { echo "this Go lists no frozen FIPS 140-3 module v1.0.0 in $dir/fips140.sum"; exit 1; }; \
        zip=${line%% *}; sum=$(echo "$line" | cut -d' ' -f2); \
        [ -f "$dir/$zip" ] || curl -fsSL -o "$dir/$zip" \
          "https://raw.githubusercontent.com/golang/go/$(go env GOVERSION)/lib/fips140/$zip"; \
        echo "$sum  $dir/$zip" | sha256sum -c -; \
        export GOFIPS140="${zip%.zip}";; \
      "") ;; \
      *) echo "FIPS=${FIPS}: want toolchain, native or empty"; exit 1;; \
    esac; \
    go version; go env GOEXPERIMENT GOFIPS140 CGO_ENABLED; \
    if [ -n "${GITALY_GO_GET}" ]; then go get ${GITALY_GO_GET}; fi; \
    go build -trimpath -o /out/setup_cgroups .; \
    go version -m /out/setup_cgroups | grep -E '^\s+build\s' || true; \
    mkdir -p /out/licenses; \
    curl -fsSL -o /out/licenses/LICENSE "${GITALY_LICENSE}"; \
    echo "${GITALY_LICENSE_SHA256}  /out/licenses/LICENSE" | sha256sum -c -

FROM ${BASE_IMAGE}

ARG BINARY=/setup_cgroups
ARG RUN_USER=0:0

LABEL org.opencontainers.image.description="Chainguard build of GitLab's gitaly-init-cgroups for Big Bang (from Gitaly's source)" \
      org.opencontainers.image.licenses="MIT"

COPY --from=build /out/licenses /licenses
COPY --from=build /out/setup_cgroups ${BINARY}

# Root, as CNG's image runs (scratch, no USER) and GitLab's chart requires: it chowns the
# Gitaly pod's cgroup to Gitaly's uid.
USER ${RUN_USER}

# CNG sets CMD, not ENTRYPOINT, and the chart sets neither; the runtime image's own
# entrypoint (kubectl's) must not wrap it.
ENTRYPOINT []
CMD ["/setup_cgroups"]
