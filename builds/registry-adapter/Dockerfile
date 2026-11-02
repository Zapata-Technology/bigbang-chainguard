# Big Bang's NeuVector registry-adapter (Iron Bank opensource/neuvector/registry-adapter)
# built from upstream's source FROM the org's images, as Iron Bank builds it (upstream's
# `make build`); Chainguard publishes no such image.
# scripts/bb-registry-adapter.py passes every input from build.yaml:
#
#   DEV_IMAGE          the org's build image (go(-fips) -dev, or kubectl(-fips) -dev), by digest
#   BASE_IMAGE         the org's runtime image (glibc-openssl(-fips), or kubectl(-fips)), by digest
#   GO_PACKAGE         Wolfi Go package to add when DEV_IMAGE has no Go ("" otherwise)
#   ADAPTER_*          upstream's source tarball, its sha256, the tag (stamped into
#                      version.go, as upstream's and Iron Bank's builds do) and the modules to raise
#   FIPS               "toolchain" (the build image's FIPS Go), "native" (Go's own FIPS
#                      140-3 module), or "" (standard)
#   BINARY, RUN_USER   Iron Bank's binary path and entrypoint, and its user (nobody, 65534)
ARG DEV_IMAGE
ARG BASE_IMAGE

FROM ${DEV_IMAGE} AS build

ARG GO_PACKAGE=""
ARG ADAPTER_SOURCE
ARG ADAPTER_SHA256
ARG ADAPTER_VERSION
ARG ADAPTER_GO_GET=""
ARG FIPS=""

USER root

# Upstream's `make build` (go build -ldflags='-s -w'), with the image's Go and
# ADAPTER_GO_GET's modules raised. It needs cgo (NeuVector's share/system package), so
# both binaries link the runtime image's glibc. Upstream builds from vendor/; raising
# modules makes it stale, so this builds with -mod=mod (proxy.golang.org) instead.
# FIPS=toolchain: the build image's Go is Chainguard's FIPS Go (go-fips), crypto through
#   OpenSSL's FIPS provider.
# FIPS=native: no FIPS toolchain in the org (kubectl-fips -dev offers only go-1.N): build
#   against Go's own FIPS 140-3 module, as builds/mattermost-operator/Dockerfile explains:
#   GOFIPS140 names the frozen module in Go's lib/fips140/fips140.sum, whose zip Wolfi
#   drops, so it comes from the Go repository at the toolchain's tag, checked by sha256.
RUN set -e; \
    command -v go >/dev/null || apk add --no-cache ${GO_PACKAGE}; \
    command -v curl >/dev/null || apk add --no-cache curl; \
    command -v gcc >/dev/null || apk add --no-cache gcc glibc-dev; \
    curl -fsSL -o /tmp/src.tgz "${ADAPTER_SOURCE}"; \
    echo "${ADAPTER_SHA256}  /tmp/src.tgz" | sha256sum -c -; \
    mkdir -p /src /out; \
    tar -xzf /tmp/src.tgz -C /src --strip-components=1; \
    cd /src; \
    sed -i -e 's/interim.*xxxx/'"${ADAPTER_VERSION}"'/' version.go; \
    grep -q "\"${ADAPTER_VERSION}\"" version.go || { echo "version.go no longer has upstream's placeholder"; exit 1; }; \
    export GOTOOLCHAIN=local GOFLAGS=-mod=mod CGO_ENABLED=1; \
    case "${FIPS}" in \
      toolchain) ;; \
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
    if [ -n "${ADAPTER_GO_GET}" ]; then go get ${ADAPTER_GO_GET}; fi; \
    go build -trimpath -ldflags '-s -w' -o /out/adapter .; \
    go version -m /out/adapter | grep -E '^\s+(build|dep\s+(golang.org/x/net|github.com/go-jose/go-jose/v4))\s' || true; \
    mkdir -p /out/licenses; cp LICENSE /out/licenses/LICENSE

FROM ${BASE_IMAGE}

ARG BINARY=/usr/local/bin/adapter
ARG RUN_USER=65534:65534

LABEL org.opencontainers.image.description="Chainguard build of the NeuVector registry adapter for Big Bang (from upstream source)" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /
COPY --from=build /out/licenses /licenses
COPY --from=build /out/adapter ${BINARY}

# Numeric, so pods with runAsNonRoot can verify it.
USER ${RUN_USER}

ENTRYPOINT ["/usr/local/bin/adapter"]
CMD []
