ARG BASE_REGISTRY=registry1.dsop.io
ARG BASE_IMAGE=google/distroless12/static
ARG BASE_TAG=nonroot

FROM mattermost/mattermost-operator:v1.25.9 as build

FROM ${BASE_REGISTRY}/${BASE_IMAGE}:${BASE_TAG}


WORKDIR /

COPY --from=build /mattermost-operator /mattermost-operator

USER nonroot:nonroot

ENTRYPOINT ["/mattermost-operator"]
