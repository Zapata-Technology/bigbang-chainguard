ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/redhat/ubi/ubi9-minimal
ARG BASE_TAG=9.7

FROM quay.io/minio/operator:v7.1.1 as upstream
FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

COPY --from=upstream /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/pki/ca-trust/extracted/pem/
COPY --from=upstream /licenses /licenses
COPY --from=upstream minio-operator /minio-operator

RUN microdnf upgrade -y --nodocs && \
microdnf install ca-certificates -y --nodocs && \
microdnf clean all && \
rm -rf /var/cache/yum /var/log/yum*

ENTRYPOINT ["/minio-operator"]
