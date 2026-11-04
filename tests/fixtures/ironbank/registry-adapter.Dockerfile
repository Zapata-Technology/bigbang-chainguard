ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/redhat/ubi/ubi9-minimal
ARG BASE_TAG=9.8

FROM registry1.dso.mil/ironbank/google/golang/ubi9/golang-1.24 AS build

USER root

ARG VERSION=0.2.9

COPY registry-adapter.tar.gz /

RUN microdnf update -y && \
    microdnf install -y make gcc && \
    mkdir /ra && \
    tar --strip-components=1 -C /ra -xvf /registry-adapter.tar.gz

WORKDIR /ra

RUN echo $VERSION && \
    sed -i -e 's/interim.*xxxx/$VERSION/g' ./version.go && \
    make build

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/cni/bin

COPY --from=build /ra/adapter /usr/local/bin/

RUN microdnf upgrade -y && \
    microdnf clean all && \
    rm -rf /var/cache/microdnf

USER nobody
ENTRYPOINT  ["/usr/local/bin/adapter"]
