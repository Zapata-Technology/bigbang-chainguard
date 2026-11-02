ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/opensource/jaegertracing/jaeger-query
ARG BASE_TAG=2.20.0

ARG BUILD_IMAGE=ironbank/google/golang/ubi9/golang-1.26
ARG BUILD_TAG=1.26.10

FROM ${BASE_REGISTRY}/${BUILD_IMAGE}:${BUILD_TAG} AS build

USER root

WORKDIR /src/tempo

COPY tempo-3.1.1.tar.gz /tmp/tempo-source.tar.gz

RUN tar -xzf /tmp/tempo-source.tar.gz -C /src/tempo --strip-components=1 && \
    go get \
        github.com/apache/thrift@v0.24.0 \
        golang.org/x/crypto@v0.56.0 \
        go.etcd.io/etcd/api/v3@v3.6.14 \
        go.etcd.io/etcd/client/pkg/v3@v3.6.14 \
        go.etcd.io/etcd/client/v3@v3.6.14 \
        google.golang.org/grpc@v1.83.1 && \
    go mod tidy && \
    go mod vendor && \
    CGO_ENABLED=0 go build -trimpath -mod=vendor \
    -ldflags="-w -X main.Version=${TEMPO_VERSION}" \
    -o ./tempo-query ./cmd/tempo-query

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

USER root

RUN microdnf upgrade -y && \
    microdnf clean all && \
    rm -rf /var/cache/dnf && \
    groupadd -g 10001 tempo-query && \
    useradd -r -u 10001 -m -s /sbin/nologin -g tempo-query tempo-query && \
  # https://github.com/grafana/tempo/blob/main/cmd/tempo-query/Dockerfile
  # Ensure /tmp dir exists, hashicorp plugins need a /tmp dir to exist.
  # mode 1777 is wide open with the sticky bit, which is the default for /tmp on any linux system
    mkdir -p /tmp && \
    chmod 1777 /tmp

COPY --from=build /src/tempo/tempo-query /tempo-query

USER tempo-query:tempo-query

ENTRYPOINT ["/tempo-query"]