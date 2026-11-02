## BUILDER IMAGE ##
ARG GITALY_IMAGE=

FROM --platform=${TARGETPLATFORM} ${GITALY_IMAGE} AS gitaly

## FINAL IMAGE ##
FROM --platform=${TARGETPLATFORM} scratch

# pull contents from builder
COPY --from=gitaly /usr/local/bin/setup_cgroups /

# Set the command to run when the container starts
CMD ["/setup_cgroups"]
