# Big Bang's mattermost-operator (Iron Bank opensource/mattermost/mattermost-operator)
# built from upstream's source FROM the org's images. Iron Bank copies the binary out
# of upstream's image onto distroless static; Chainguard publishes no such image.
# scripts/bb-mattermost-operator.py passes every input from build.yaml:
#
#   DEV_IMAGE          the org's build image (go(-fips) -dev, or kubectl(-fips) -dev), by digest
#   BASE_IMAGE         the org's runtime image (glibc-openssl(-fips), or kubectl(-fips)), by digest
#   GO_PACKAGE         Wolfi Go package to add when DEV_IMAGE has no Go ("" otherwise)
#   OPERATOR_*         upstream's source tarball, its sha256, the tag's commit and time
#                      (upstream's version ldflags) and the modules to raise
#   FIPS               "toolchain" (the build image's FIPS Go, like upstream's Dockerfile.fips),
#                      "native" (Go's own FIPS 140-3 module), or "" (standard)
#   BINARY, RUN_USER   Iron Bank's binary path and entrypoint, and its user (nonroot, 65532)
ARG DEV_IMAGE
ARG BASE_IMAGE

FROM ${DEV_IMAGE} AS build

ARG GO_PACKAGE=""
ARG OPERATOR_SOURCE
ARG OPERATOR_SHA256
ARG OPERATOR_COMMIT
ARG OPERATOR_BUILD_TIME
ARG OPERATOR_GO_GET=""
ARG FIPS=""

USER root

# Upstream's Makefile build (main.go, its version ldflags), with the image's Go and
# OPERATOR_GO_GET's modules raised. The standard binary is static (CGO_ENABLED=0).
# FIPS=toolchain: the build image's Go is Chainguard's FIPS Go (go-fips): build like
#   upstream's Dockerfile.fips (cgo, -tags=requirefips), crypto through OpenSSL's FIPS provider.
# FIPS=native: no FIPS toolchain in the org (kubectl-fips -dev offers only go-1.N): build
#   against Go's own FIPS 140-3 module (GOFIPS140=v1.0.0, the frozen module, which the
#   Go command checks against its lib/fips140/fips140.sum; Wolfi's Go ships the sum but
#   not the zip, so it comes from the module proxy), which also makes fips140=on the
#   binary's default GODEBUG.
RUN set -e; \
    command -v go >/dev/null || apk add --no-cache ${GO_PACKAGE}; \
    command -v curl >/dev/null || apk add --no-cache curl; \
    if [ "${FIPS}" = toolchain ]; then command -v gcc >/dev/null || apk add --no-cache gcc glibc-dev; fi; \
    curl -fsSL -o /tmp/src.tgz "${OPERATOR_SOURCE}"; \
    echo "${OPERATOR_SHA256}  /tmp/src.tgz" | sha256sum -c -; \
    mkdir -p /src /out; \
    tar -xzf /tmp/src.tgz -C /src --strip-components=1; \
    cd /src; \
    export GOTOOLCHAIN=local GOFLAGS=-mod=mod CGO_ENABLED=0; \
    tags=""; \
    case "${FIPS}" in \
      toolchain) export CGO_ENABLED=1; tags="-tags=requirefips";; \
      native) sums="$(go env GOROOT)/lib/fips140/fips140.sum"; grep -v '^#' "$sums" || true; \
        if grep -q '^v1\.0\.0\.zip ' "$sums"; then export GOFIPS140=v1.0.0; \
        else echo "this Go lists no frozen FIPS 140-3 module v1.0.0 in $sums"; exit 1; fi;; \
      "") ;; \
      *) echo "FIPS=${FIPS}: want toolchain, native or empty"; exit 1;; \
    esac; \
    go version; go env GOEXPERIMENT GOFIPS140 CGO_ENABLED; \
    if [ -n "${OPERATOR_GO_GET}" ]; then go get ${OPERATOR_GO_GET}; fi; \
    go build -trimpath ${tags} -o /out/mattermost-operator \
      -ldflags "-s -w -X 'github.com/mattermost/mattermost-operator/version.buildTime=${OPERATOR_BUILD_TIME}' -X 'github.com/mattermost/mattermost-operator/version.buildHash=${OPERATOR_COMMIT}'" \
      ./main.go; \
    go version -m /out/mattermost-operator | grep -E '^\s+(build|dep\s+golang.org/x/net)\s' || true; \
    mkdir -p /out/licenses; cp LICENSE /out/licenses/LICENSE

FROM ${BASE_IMAGE}

ARG BINARY=/mattermost-operator
ARG RUN_USER=65532:65532

LABEL org.opencontainers.image.description="Chainguard build of the Mattermost Operator for Big Bang (from upstream source)"

WORKDIR /
COPY --from=build /out/licenses /licenses
COPY --from=build /out/mattermost-operator ${BINARY}

# Numeric, so pods with runAsNonRoot can verify it.
USER ${RUN_USER}

ENTRYPOINT ["/mattermost-operator"]
CMD []
