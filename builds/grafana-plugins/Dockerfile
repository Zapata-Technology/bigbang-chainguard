# Big Bang's grafana-plugins image, rebuilt FROM the Chainguard org's grafana or
# grafana-fips image. scripts/bb-build.py grafana-plugins passes every input from
# build.yaml, which it generates from Iron Bank's own hardening_manifest.yaml and
# Dockerfile (repo1.dso.mil/dsop/big-bang/grafana/grafana-plugins):
#
#   BASE_IMAGE   runtime image, pinned by digest
#   DEV_IMAGE    its -dev twin (has a shell and apk); the plugins are unpacked there
#   PLUGINS      "<url> <sha256> <dir>" entries separated by ";"
#   EMPTY_DIRS   directories Iron Bank creates without a plugin in them
#   REBUILDS     "<dir> <source> <sha256> <executable> <patch> <module@version>..." entries
#                separated by ";": plugins whose Go backend is rebuilt from source
ARG BASE_IMAGE
ARG DEV_IMAGE

FROM ${DEV_IMAGE} AS plugins

ARG PLUGINS
ARG EMPTY_DIRS=""
ARG REBUILDS=""

USER root

COPY patches/ /patches/

RUN set -e; \
    apk add --no-cache curl unzip coreutils; \
    mkdir -p /var/lib/bb-plugins; \
    for d in ${EMPTY_DIRS}; do mkdir -p "$d"; done; \
    echo "${PLUGINS}" | tr ';' '\n' | while read -r url sum dir; do \
      [ -n "$url" ] || continue; \
      curl -fsSL -o /tmp/plugin.zip "$url"; \
      echo "$sum  /tmp/plugin.zip" | sha256sum -c -; \
      mkdir -p "$dir"; \
      unzip -q /tmp/plugin.zip -d "$dir"; \
      rm -f /tmp/plugin.zip; \
    done; \
    chown -R 65532:65532 /var/lib/bb-plugins

# Replace a plugin's prebuilt backends (every OS and arch, all built with the old Go)
# with one built here, and drop its MANIFEST.txt: it no longer matches, and Grafana
# refuses a modified plugin but loads an allowed unsigned one.
RUN set -e; \
    [ -n "${REBUILDS}" ] || exit 0; \
    apk add --no-cache go patch; \
    export GOTOOLCHAIN=local CGO_ENABLED=0 GOFLAGS=-mod=mod; \
    echo "${REBUILDS}" | tr ';' '\n' | while read -r dir src sum exe fix mods; do \
      [ -n "$dir" ] || continue; \
      root=$(dirname "$(find "$dir" -name plugin.json | awk '{ print length, $0 }' | sort -n | head -1 | cut -d' ' -f2-)"); \
      curl -fsSL -o /tmp/src.tgz "$src"; \
      echo "$sum  /tmp/src.tgz" | sha256sum -c -; \
      rm -rf /tmp/src; mkdir -p /tmp/src; \
      tar -xzf /tmp/src.tgz -C /tmp/src --strip-components=1; \
      (cd /tmp/src; patch -p1 < "/patches/${fix#patches/}"; \
       [ -z "$mods" ] || go get $mods; \
       go build -trimpath -ldflags "-s -w" -o "/tmp/${exe}_linux_$(go env GOARCH)" ./pkg); \
      rm -f "$root/${exe}"_* "$root/MANIFEST.txt"; \
      install -m 0755 "/tmp/${exe}_linux_$(go env GOARCH)" "$root/"; \
      rm -rf /tmp/src /tmp/src.tgz; \
    done

FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.description="Chainguard Grafana with Big Bang's Iron Bank grafana-plugins set"

COPY --from=plugins --chown=65532:65532 /var/lib/bb-plugins /var/lib/bb-plugins
