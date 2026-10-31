# Iron Bank's opensource/prometheus/jmx-exporter rebuilt FROM the org's busybox or
# busybox-fips -dev image, for SonarQube's inject-prometheus-exporter init container.
# That container runs `/bin/sh -c "curl -s 'file://<JAR_DIR>/<JAR_NAME>' --output
# /data/jmx_prometheus_javaagent.jar -v"` as uid 1000 with a read-only root, so the
# image needs sh (the -dev image's busybox), curl and the jar at Iron Bank's path.
# scripts/bb-jmx-exporter.py passes every input from build.yaml, which it generates from
# Iron Bank's own hardening_manifest.yaml and Dockerfile
# (repo1.dso.mil/dsop/opensource/prometheus/jmx-exporter):
#
#   DEV_IMAGE            busybox(-fips) -dev image, pinned by digest
#   PACKAGES             Wolfi packages (curl)
#   JAR_URL, JAR_SHA256  the javaagent jar Iron Bank's manifest lists, and its sha256
#   JAR_DIR, JAR_NAME    where Iron Bank puts it (the chart's downloadURL names this path)
#   USER_ID              the uid Iron Bank's image runs as
ARG DEV_IMAGE

FROM ${DEV_IMAGE}

ARG PACKAGES
ARG JAR_URL
ARG JAR_SHA256
ARG JAR_DIR=/opt/jmx_exporter
ARG JAR_NAME
ARG USER_ID=1000

USER root

# World-readable: the chart runs the copy as uid 1000, gid 0, not as the image's user.
RUN set -e; \
    apk add --no-cache ${PACKAGES}; \
    mkdir -p "${JAR_DIR}"; \
    curl -fsSL -o "${JAR_DIR}/${JAR_NAME}" "${JAR_URL}"; \
    echo "${JAR_SHA256}  ${JAR_DIR}/${JAR_NAME}" | sha256sum -c -; \
    chown -R "${USER_ID}:${USER_ID}" "${JAR_DIR}"; \
    chmod 0755 "${JAR_DIR}"; \
    chmod 0644 "${JAR_DIR}/${JAR_NAME}"

LABEL org.opencontainers.image.description="Chainguard rebuild of Iron Bank's jmx-exporter (the Prometheus JMX javaagent jar, sh and curl)"

WORKDIR ${JAR_DIR}

# The chart passes its own command; Iron Bank's java entrypoint runs the standalone
# exporter, which Big Bang doesn't use.
ENTRYPOINT []
CMD ["/bin/sh"]

# Numeric, so pods with runAsNonRoot can verify it.
USER ${USER_ID}:${USER_ID}
