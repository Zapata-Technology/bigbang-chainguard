# Big Bang's tempo-query (Iron Bank opensource/grafana/tempo-query) built from
# grafana/tempo's source FROM the org's images. Iron Bank puts the binary on its
# jaeger-query image; Chainguard publishes no tempo-query image.
# scripts/bb-tempo-query.py passes every input from build.yaml:
#
#   DEV_IMAGE          the org's build image (go(-fips) -dev, or kubectl(-fips) -dev), by digest
#   BASE_IMAGE         the org's runtime image (glibc-openssl(-fips), or kubectl(-fips)), by digest
#   GO_PACKAGE         Wolfi Go package to add when DEV_IMAGE has no Go ("" otherwise)
#   TEMPO_*            upstream's source tarball, its sha256, the Go package to build
#                      (./cmd/tempo-query) and the modules to raise
#   FIPS               "toolchain" (the build image's FIPS Go, cgo), "native" (Go's own
#                      FIPS 140-3 module), or "" (standard)
#   BINARY, RUN_USER   Iron Bank's binary path and entrypoint, and its user (10001)
ARG DEV_IMAGE
ARG BASE_IMAGE

FROM ${DEV_IMAGE} AS build

ARG GO_PACKAGE=""
ARG TEMPO_SOURCE
ARG TEMPO_SHA256
ARG TEMPO_PACKAGE=./cmd/tempo-query
ARG TEMPO_GO_GET=""
ARG FIPS=""

USER root

# Upstream's `make tempo-query`, with the image's Go and TEMPO_GO_GET's modules raised.
# Upstream builds from its vendor/ directory; raising modules makes vendor/ stale, so this
# builds with -mod=mod (proxy.golang.org) instead. Upstream's version ldflags set
# variables tempo-query doesn't have, so there are none. The standard binary is static.
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
    curl -fsSL -o /tmp/src.tgz "${TEMPO_SOURCE}"; \
    echo "${TEMPO_SHA256}  /tmp/src.tgz" | sha256sum -c -; \
    mkdir -p /src /out; \
    tar -xzf /tmp/src.tgz -C /src --strip-components=1; \
    cd /src; \
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
    if [ -n "${TEMPO_GO_GET}" ]; then go get ${TEMPO_GO_GET}; fi; \
    go build -trimpath -ldflags "-s -w" -o /out/tempo-query "${TEMPO_PACKAGE}"; \
    go version -m /out/tempo-query | grep -E '^\s+(build|dep\s+(golang.org/x/(net|text)|google.golang.org/grpc|github.com/apache/thrift))\s' || true; \
    mkdir -p /out/licenses; cp LICENSE /out/licenses/LICENSE

FROM ${BASE_IMAGE}

ARG BINARY=/tempo-query
ARG RUN_USER=10001:10001

LABEL org.opencontainers.image.description="Chainguard build of Grafana Tempo's tempo-query for Big Bang (from upstream source)" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

WORKDIR /
COPY --from=build /out/licenses /licenses
COPY --from=build /out/tempo-query ${BINARY}

# Numeric, so pods with runAsNonRoot can verify it.
USER ${RUN_USER}

ENTRYPOINT ["/tempo-query"]
CMD []
