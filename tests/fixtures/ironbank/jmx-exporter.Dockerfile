ARG BASE_REGISTRY=registry1.dso.mil
ARG BASE_IMAGE=ironbank/redhat/ubi/ubi9
ARG BASE_TAG=9.8

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}

ARG USER=java
ARG GROUP=java
ARG VERSION=1.6.0

ENV JAVA_HOME=/usr/lib/jvm/jre-17-openjdk

USER root

COPY jmx_prometheus_javaagent-${VERSION}.jar jmx_prometheus_standalone-${VERSION}.jar jmx_exporter-${VERSION}.tar.gz LICENSE /opt/jmx_exporter/

RUN dnf upgrade -y && \
    dnf install -y \
        tar \
        gzip \
        shadow-utils \
        curl \
        java-17-openjdk-headless --allowerasing && \
    tar xzvf /opt/jmx_exporter/jmx_exporter-${VERSION}.tar.gz -C /opt/jmx_exporter/ --strip-components=1 jmx_exporter-${VERSION}/examples/ && \
    dnf remove -y tar && \
    dnf clean all && \
    rm -rf /var/cache/dnf /var/log/dnf*

RUN groupadd ${GROUP} && \
    useradd -g ${GROUP} ${USER} && \
    chown -R ${USER}:${GROUP} /opt/jmx_exporter && \
    ln -s /opt/jmx_exporter/jmx_prometheus_standalone-${VERSION}.jar /opt/jmx_exporter/jmx_prometheus_standalone.jar && \
    rm -rf /opt/jmx_exporter/jmx_exporter-${VERSION}.tar.gz

USER ${USER}

EXPOSE 5556

WORKDIR /opt/jmx_exporter

USER 1000

ENTRYPOINT ["java", "-jar", "jmx_prometheus_standalone-1.6.0.jar"]
CMD ["5556", "examples/standalone_sample_config.yml"]