# BASE_REGISTRY, BASE_IMAGE, and BASE_TAG are overridden by the IB hardening
# pipeline at build time. The defaults here should also produce a valid build.
# For local builds you'll also need the mc binary — see hardening_manifest.yaml.
ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/ironbank-pipelines/pipeline-runner-alpine
ARG BASE_TAG=26

# Multi-stage sources are hardcoded to registry1 so that Renovate can auto-bump
# their tags. IB docs only require BASE_REGISTRY/BASE_IMAGE/BASE_TAG for the
# final base image, not for COPY --from sources.
FROM registry1.dso.mil/ironbank/opensource/velero/velero:v1.18.4 AS velero
FROM registry1.dso.mil/ironbank/opensource/kyverno/kyvernocli:v1.19.1 AS kyverno
FROM registry.k8s.io/kubectl:v1.37.1 AS kubectl
FROM ghcr.io/kyverno/chainsaw:v0.2.15 AS chainsaw

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

USER root

COPY --from=kubectl /bin/kubectl /usr/bin/kubectl
RUN chmod +x /usr/bin/kubectl

# Copy mc (MinIO Client — S3-compatible object storage CLI)
COPY mc /usr/bin/mc
RUN chmod +x /usr/bin/mc

# Copy velero (Kubernetes backup/restore CLI for disaster recovery)
COPY --from=velero /velero /usr/bin/velero

# Copy kyverno CLI — used in gluon script tests for the kyverno-policies chart.
# The kyvernocli IB image is distroless (no bash/kubectl) so we pull the binary
# here to pair it with the bash+kubectl already in this image.
COPY --from=kyverno /ko-app/kubectl-kyverno /usr/bin/kyverno

# Copy chainsaw — used in gluon script tests for the kyverno-policies chart.
# Not yet available in IronBank; using ghcr.io source for now.
COPY --from=chainsaw /ko-app/chainsaw /usr/bin/chainsaw

# Install curl
RUN apk update && \
    apk add curl

# Create non root user and configure to run as rootless
RUN addgroup devops                                         && \
    adduser devops-user -G devops -u 1000 -D -s /bin/bash   && \
    chmod 0750 /home/devops-user                            && \
    mkdir -p /run/containers/1000                           &&\
    chown -R devops-user:devops /run/containers/1000

# Run as non root user
USER devops-user
