# Big Bang's MinIO Operator and its sidecar (Iron Bank opensource/minio/operator and
# operator-sidecar, both archived) built from minio/operator's source FROM the org's
# images: scripts/bb-build.py minio-operator and minio-operator-sidecar both build with
# this file (their hook: scripts/buildhooks/minio.py). Chainguard publishes neither image.
# Inputs from build.yaml:
#
#   DEV_IMAGE          the org's build image (go(-fips) -dev, or kubectl(-fips) -dev), by digest
#   BASE_IMAGE         the org's runtime image (glibc-openssl(-fips), or kubectl(-fips)), by digest
#   GO_PACKAGE         Wolfi Go package to add when DEV_IMAGE has no Go ("" otherwise)
#   MINIO_*            upstream's source tarball and sha256, the module directory (. or
#                      sidecar) and Go package, upstream's version ldflags, the `replace`
#                      directives to drop and the modules to raise
#   FIPS               "toolchain" (the build image's FIPS Go), "native" (Go's own FIPS
#                      140-3 module), or "" (standard)
#   BINARY, RUN_USER   Iron Bank's binary path and entrypoint, and the user (65532)
ARG DEV_IMAGE
ARG BASE_IMAGE

FROM ${DEV_IMAGE} AS build

ARG GO_PACKAGE=""
ARG MINIO_SOURCE
ARG MINIO_SHA256
ARG MINIO_MODULE=.
ARG MINIO_PACKAGE
ARG MINIO_LDFLAGS="-s -w"
ARG MINIO_DROP_REPLACE=""
ARG MINIO_GO_GET=""
ARG FIPS=""

USER root

# Upstream's Makefile build (CGO_ENABLED=0, -trimpath, its version ldflags), with the
# image's Go. Upstream's go.mod pins golang.org/x/crypto with a `replace` to a release with
# Critical CVEs, so MINIO_DROP_REPLACE drops it before MINIO_GO_GET raises the modules.
# FIPS=toolchain: the build image's Go is Chainguard's FIPS Go (go-fips): cgo, crypto
#   through OpenSSL's FIPS provider.
# FIPS=native: no FIPS toolchain in the org (kubectl-fips -dev offers only go-1.N): build
#   against Go's own FIPS 140-3 module, as builds/mattermost-operator/Dockerfile explains:
#   GOFIPS140 names the frozen module in Go's lib/fips140/fips140.sum, whose zip Wolfi
#   drops, so it comes from the Go repository at the toolchain's tag, checked by sha256.
RUN set -e; \
    command -v go >/dev/null || apk add --no-cache ${GO_PACKAGE}; \
    command -v curl >/dev/null || apk add --no-cache curl; \
    if [ "${FIPS}" = toolchain ]; then command -v gcc >/dev/null || apk add --no-cache gcc glibc-dev; fi; \
    curl -fsSL -o /tmp/src.tgz "${MINIO_SOURCE}"; \
    echo "${MINIO_SHA256}  /tmp/src.tgz" | sha256sum -c -; \
    mkdir -p /src /out; \
    tar -xzf /tmp/src.tgz -C /src --strip-components=1; \
    cd "/src/${MINIO_MODULE}"; \
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
    for m in ${MINIO_DROP_REPLACE}; do go mod edit -dropreplace="$m"; done; \
    if [ -n "${MINIO_GO_GET}" ]; then go get ${MINIO_GO_GET}; fi; \
    go build -trimpath -ldflags "${MINIO_LDFLAGS}" -o /out/program "${MINIO_PACKAGE}"; \
    go version -m /out/program | grep -E '^\s+(build|dep\s+golang.org/x/(crypto|net))\s' || true; \
    mkdir -p /out/licenses; cp /src/LICENSE /src/CREDITS /out/licenses/

FROM ${BASE_IMAGE} AS runtime

ARG BINARY
ARG RUN_USER=65532:65532

LABEL org.opencontainers.image.description="Chainguard build of the MinIO Operator for Big Bang (from upstream source)" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

WORKDIR /
COPY --from=build /out/licenses /licenses
COPY --from=build /out/program ${BINARY}

# Numeric, so pods with runAsNonRoot can verify it.
USER ${RUN_USER}

# An exec-form ENTRYPOINT can't take BINARY from an ARG, so each image is its own final
# stage (build.yaml's `target`) with Iron Bank's entrypoint; each smoke test runs it.
FROM runtime AS operator
ENTRYPOINT ["/minio-operator"]

FROM runtime AS sidecar
ENTRYPOINT ["/minio-operator-sidecar"]
