ARG CI_REGISTRY_IMAGE="registry.gitlab.com/gitlab-org/build/cng"
ARG GO_TAG="master"
ARG GO_IMAGE="$CI_REGISTRY_IMAGE/gitlab-go"
ARG PYTHON_TAG="3.12.12"
ARG PYTHON_IMAGE="$CI_REGISTRY_IMAGE/gitlab-python"
ARG GITLAB_BASE_IMAGE=

FROM --platform=${TARGETPLATFORM} ${GO_IMAGE}:${GO_TAG} AS golang

FROM --platform=${TARGETPLATFORM} ${PYTHON_IMAGE}:${PYTHON_TAG} AS python

FROM --platform=${TARGETPLATFORM} ${GITLAB_BASE_IMAGE} AS builder

COPY --from=golang /usr/local/go /usr/local/go

ARG GITALY_SERVER_VERSION=v1.83.0
ARG GITALY_GIT_REPO_URL
ARG BUILD_DIR=/tmp/build
ARG GITLAB_NAMESPACE="gitlab-org"

ARG BUNDLE_OPTIONS="--jobs 4"
ARG CI_SERVER_FQDN
ARG CI_JOB_TOKEN
ARG CI_SERVER_PROTOCOL
ENV GOTOOLCHAIN=local

# install runtime deps. openssh-client is required so that SSH client binaries
# are present for repository mirroring.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libicu76 iproute2 openssh-client xtail

COPY shared/build-scripts/ /build-scripts

ARG CACHE_BUSTER=false

# BUILD_TOKEN is a read_repository token mounted as a BuildKit secret by
# .with-build-token on the security mirror. When unset (canonical, QA mirror,
# dev mirror, forks), the clone falls back to CI_JOB_TOKEN.
#
# Install build dependencies required to build Git and libgit2.
RUN --mount=type=secret,id=build_token,env=BUILD_TOKEN \
    buildDeps=' \
    make \
    cmake \
    gcc \
    g++ \
    git \
    pkg-config \
    libssl-dev \
    meson \
    ninja-build \
    zlib1g-dev' && \
    runDeps=' \
    libicu-dev \
    libpcre2-dev \
    libcurl4-gnutls-dev' && \
    apt-get update && \
    apt-get install -y --no-install-recommends $buildDeps $runDeps && \
    ln -sf /usr/local/go/bin/go /usr/local/go/bin/gofmt /usr/local/go/bin/godoc /usr/local/bin/ && \
    mkdir -p ${BUILD_DIR} && \
    cd ${BUILD_DIR} && \
    export GITLAB_NAMESPACE_RAW="$(echo -n "$GITLAB_NAMESPACE" | sed 's/%2F/\//')" && \
    if [ -n "${BUILD_TOKEN}" ]; then GIT_AUTH="oauth2:${BUILD_TOKEN}" ; else GIT_AUTH="gitlab-ci-token:${CI_JOB_TOKEN}" ; fi && \
    echo "Downloading source code from ${CI_SERVER_PROTOCOL}://${CI_SERVER_FQDN}/${GITLAB_NAMESPACE_RAW}/gitaly.git" && \
    git clone --filter=tree:0 "${CI_SERVER_PROTOCOL}://${GIT_AUTH}@${CI_SERVER_FQDN}/${GITLAB_NAMESPACE_RAW}/gitaly.git" . && \
    echo "Git resetting to commit sha=${GITALY_SERVER_VERSION}" && \
    git reset --hard ${GITALY_SERVER_VERSION} && \
    if [ -n "${GITALY_GIT_REPO_URL}" ]; then export GIT_REPO_URL="${GITALY_GIT_REPO_URL}" ; fi && \
    make install WITH_BUNDLED_GIT=YesPlease USE_MESON=YesPlease && \
    # Build `gitaly-init-cgroups`
    CGO_ENABLED=0 go build -C "${BUILD_DIR}/tools/gitaly-init-cgroups" -trimpath -o /usr/local/bin/setup_cgroups . && \
    # Copy binaries to /target
    mkdir /target && \
    cp -R --parents \
      /usr/local/bin/gitaly* \
      /usr/local/bin/praefect \
      /usr/local/bin/setup_cgroups \
      /target

## FINAL IMAGE ##
ARG GITLAB_BASE_IMAGE
FROM --platform=${TARGETPLATFORM} ${GITLAB_BASE_IMAGE}

ARG GITLAB_USER=git

ARG GIT_FILTER_REPO_VERSION="2.47.0"
COPY --from=python /usr/local/bin /usr/local/bin/
COPY --from=python /usr/local/lib /usr/local/lib/
COPY --from=python /usr/local/include /usr/local/include/
ENV PYTHONPATH=/usr/local/lib/python3.12/site-packages

# install git-filter-repo.
RUN ldconfig \
    && pip3 install git-filter-repo==${GIT_FILTER_REPO_VERSION} \
    && pip3 cache purge \
    && find /usr/local/lib/python3.12 -name '__pycache__' -type d -print -exec rm -r {} +

# install runtime deps. openssh-client is required so that SSH client binaries
# are present for repository mirroring.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libpcre2-16-0 libpcre2-32-0 libpcre2-posix3 libcurl3t64-gnutls \
      libicu76 iproute2 openssh-client xtail \
      bzip2 gzip \
    && rm -rf /var/lib/apt/lists/*

# create gitlab user
# openssh daemon does not allow locked user to login, change ! to *
RUN adduser --disabled-password --gecos 'GitLab' ${GITLAB_USER} && \
      sed -i "s/${GITLAB_USER}:!/${GITLAB_USER}:*/" /etc/shadow

# configure runtime environment, with permissions
RUN install -d -o ${GITLAB_USER} -g ${GITLAB_USER} -m 755 /home/${GITLAB_USER}/repositories && \
    install -d -o ${GITLAB_USER} -g ${GITLAB_USER} -m 755 /home/${GITLAB_USER}/custom_hooks && \
    install -d -o ${GITLAB_USER} -m 755 /etc/gitaly && \
    install -o ${GITLAB_USER} -g ${GITLAB_USER} -m 0640 /dev/null /etc/gitaly/config.toml && \
    install -d -o ${GITLAB_USER} -m 755 /srv/gitlab-shell && \
    install -d -o ${GITLAB_USER} -m 755 /var/log/gitaly && \
    install -o ${GITLAB_USER} -g ${GITLAB_USER} -m 0644 /dev/null /var/log/gitaly/gitaly.log && \
    install -o ${GITLAB_USER} -g ${GITLAB_USER} -m 0644 /dev/null /var/log/gitaly/gitlab-shell.log

# add default configuration template
COPY --chown=${GITLAB_USER}:${GITLAB_USER} config.toml /etc/gitaly/config.toml.tpl

# pull contents from builder
COPY --from=builder /target/ /

# Add scripts
COPY scripts/ /scripts/

# ensure explicit permissions on scripts
RUN chmod -R =rx /scripts/

# set runtime user
USER ${GITLAB_USER}:${GITLAB_USER}

ENV CONFIG_TEMPLATE_DIRECTORY=/etc/gitaly

CMD ["/scripts/process-wrapper"]

VOLUME /var/log/gitaly

EXPOSE 8075

HEALTHCHECK --interval=30s --timeout=10s --retries=5 \
CMD /scripts/healthcheck
