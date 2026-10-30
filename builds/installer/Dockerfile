# bb-install.py and everything it needs in one image, so a user needs only a container
# runtime and a kubeconfig. Built FROM the org's kubectl or kubectl-fips -dev image (the
# same digest builds/base pins); scripts/bb-installer.py passes:
#
#   DEV_IMAGE   kubectl(-fips) -dev image, pinned by digest
#   PACKAGES    Wolfi packages: Python and PyYAML, Helm, git (for --git-mirror)
#
# The build context is the repository root (.dockerignore keeps work/, .git and tests out):
# the overlays, Flux, SeaweedFS and scripts are copied in, and Big Bang's chart is
# downloaded at build time so an install doesn't need repo1 for it.
ARG DEV_IMAGE

FROM ${DEV_IMAGE}

ARG PACKAGES

USER root
RUN apk add --no-cache ${PACKAGES}

COPY --chown=65532:65532 . /bigbang-chainguard
WORKDIR /bigbang-chainguard
RUN python3 scripts/bb-install.py chart && chown -R 65532:65532 work

LABEL org.opencontainers.image.description="bigbang-chainguard installer: bb-install.py with kubectl, helm and the overlays"

ENV HOME=/home/nonroot
USER 65532:65532
ENTRYPOINT ["python3", "/bigbang-chainguard/scripts/bb-install.py"]
CMD ["--help"]
