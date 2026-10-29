ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/opensource/grafana/grafana
ARG BASE_TAG=13.2.3

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

COPY ["*.zip", "/tmp/build-files/"]

USER root

RUN microdnf upgrade -y && \
    microdnf install -y --nodocs unzip && \
    mkdir -p {/var/lib/bb-plugins/polystat-panel,/var/lib/bb-plugins/piechart-panel,/var/lib/bb-plugins/redis-datasource} && \
    unzip /tmp/build-files/grafana-polystat-panel.zip -d /var/lib/bb-plugins/polystat-panel && \
    unzip /tmp/build-files/redis-datasource.zip -d /var/lib/bb-plugins/redis-datasource && \
    microdnf remove -y unzip && \
    microdnf clean all && \
    rm -rf /var/cache/dnf /tmp/build-files/*.zip

USER grafana