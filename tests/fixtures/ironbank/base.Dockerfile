ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/redhat/ubi/ubi9-minimal
ARG BASE_TAG=9.8

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

# Comes with gnutls & curl-minimal
RUN microdnf upgrade -y && \
    microdnf install -y --nodocs \
        jq \
        pinentry \
        findutils \
        which && \
    microdnf clean all && \
    rm -rf /var/cache/yum /var/log/yum*

COPY --chown=root:root --chmod=755 kubectl.amd64 /usr/local/bin/kubectl
COPY --chown=root:root --chmod=755 yq.amd64 /usr/local/bin/yq
COPY --chown=root --chmod=700 ./scripts /tmp/scripts/

# Install kubectl, yq & Run CCE tasks
RUN groupadd -g 1000 base && \
    useradd -r -u 1000 -m -s /sbin/nologin -g base base && \
    /tmp/scripts/CCE-83434-1.sh && \
    /tmp/scripts/CCE-84036-3.sh && \
    /tmp/scripts/CCE-84038-9.sh && \
    /tmp/scripts/CCE-85888-6.sh && \
    /tmp/scripts/CCE-86534-5.sh && \
    rm -rf /tmp/scripts

USER base