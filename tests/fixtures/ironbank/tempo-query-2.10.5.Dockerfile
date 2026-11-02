ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/opensource/jaegertracing/jaeger-query
ARG BASE_TAG=1.76.0

FROM grafana/tempo-query:2.10.5 AS builder
FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

COPY --from=builder --chown=root:root /tempo-query /tempo-query

USER root

RUN microdnf -y --nodocs update && \
    microdnf clean all && \
    rm -rf /var/cache/dnf && \
    groupadd -g 10001 -r tempo && \
    useradd -u 10001 -r tempo -g tempo && \
  # https://github.com/grafana/tempo/blob/main/cmd/tempo-query/Dockerfile
  # Ensure /tmp dir exists, hashicorp plugins need a /tmp dir to exist.
  # mode 1777 is wide open with the sticky bit, which is the default for /tmp on any linux system
    mkdir -p /tmp && \
    chmod 1777 /tmp

USER 10001:10001
ENTRYPOINT ["/tempo-query"]