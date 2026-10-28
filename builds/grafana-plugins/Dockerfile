# Big Bang's grafana-plugins image, rebuilt FROM the Chainguard org's grafana or
# grafana-fips image. scripts/bb-grafana-plugins.py passes every input from
# build.yaml, which it generates from Iron Bank's own hardening_manifest.yaml and
# Dockerfile (repo1.dso.mil/dsop/big-bang/grafana/grafana-plugins):
#
#   BASE_IMAGE   runtime image, pinned by digest
#   DEV_IMAGE    its -dev twin (has a shell and apk); the plugins are unpacked there
#   PLUGINS      "<url> <sha256> <dir>" entries separated by ";"
#   EMPTY_DIRS   directories Iron Bank creates without a plugin in them
ARG BASE_IMAGE
ARG DEV_IMAGE

FROM ${DEV_IMAGE} AS plugins

ARG PLUGINS
ARG EMPTY_DIRS=""

USER root

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

FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.description="Chainguard Grafana with Big Bang's Iron Bank grafana-plugins set"

COPY --from=plugins --chown=65532:65532 /var/lib/bb-plugins /var/lib/bb-plugins
