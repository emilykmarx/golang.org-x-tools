# Prometheus' Alertmanager, built under argmatch and contexttrack, like prometheus/prombench-argmatch.Dockerfile (whose
# comments describe the ARGMATCH_* args). Build from the parent of the checkouts ($CT), as paramtrack's testbeds.py
# does:
#   docker build -f <this file> -t alertmanager-conftamer:v0.27.0 .
# The checkout (alertmanager-paramtrack) is upstream's v0.27.0, the version prombench runs
# (prombench/manifests/cluster-infra/5a_alertmanager_deployment.yaml).
ARG BUILDER=prometheus-builder:conftamer
ARG STOCK=quay.io/prometheus/alertmanager:v0.27.0

FROM ${BUILDER} AS build
ARG ARGMATCH_FUNC=main.main
ARG ARGMATCH_MATCH=
ARG ARGMATCH_DEPTH=5
ARG ARGMATCH_MAXELEMS=64
ARG ARGMATCH_MAXFIELDS=-1
ARG ARGMATCH_MAXTIME=0
ARG ARGMATCH_MAXMEM=0
ARG ARGMATCH_MAXCALLS=3
ARG ARGMATCH_CALLINTERVAL=0
ARG ARGMATCH_MATCHFILE=
ARG ARGMATCH_STACKLOG=
ARG ARGMATCH_RECVONLY=
ARG ARGMATCH_EXACT=true
ARG ARGMATCH_IGNORECLOSURES=true
ARG ARGMATCH_IGNORELIBS=true
ARG ARGMATCH_IGNORETYPES=
ARG ARGMATCH_OUT=
ARG ARGMATCH_STATS=false
# Patch Go's net/http (on top of the builder's own Go patch) to log HTTP messages, and Alertmanager's two routers to log
# their routes: prometheus/common/route (the UI, /-/ready, /-/reload, ...) and go-openapi/runtime's (the go-swagger
# generated /api/v2, where Prometheus posts alerts); see conftamer/contexttrack/README.md. Nothing is logged unless
# CONFTAMER_EVENTS is set.
COPY conftamer/contexttrack/go-inlibrary.patch conftamer/contexttrack/prometheus-common-route.patch \
     conftamer/contexttrack/go-openapi-route.patch /patches/
RUN cd /usr/local/go && patch -p4 < /patches/go-inlibrary.patch
COPY golang.org-x-tools/gopls/conftamer/stacklog/ /golang.org-x-tools/gopls/conftamer/stacklog/
COPY dump_ancestors/argmatch/ /dump_ancestors/argmatch/
RUN cd /dump_ancestors/argmatch && go build -o argmatch .
COPY alertmanager-paramtrack/ /src/
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    cd /src && \
    go mod download github.com/prometheus/common && \
    common=$(go list -m -f '{{.Dir}}' github.com/prometheus/common) && \
    rm -rf /common-conftamer && cp -r "$common" /common-conftamer && chmod -R u+w /common-conftamer && \
    (cd /common-conftamer && patch -p4 < /patches/prometheus-common-route.patch) && \
    go mod edit -replace=github.com/prometheus/common=/common-conftamer && \
    go mod download github.com/go-openapi/runtime && \
    openapi=$(go list -m -f '{{.Dir}}' github.com/go-openapi/runtime) && \
    rm -rf /openapi-conftamer && cp -r "$openapi" /openapi-conftamer && chmod -R u+w /openapi-conftamer && \
    (cd /openapi-conftamer && patch -p1 < /patches/go-openapi-route.patch) && \
    go mod edit -replace=github.com/go-openapi/runtime=/openapi-conftamer && \
    ARGMATCH_FUNC="${ARGMATCH_FUNC}" ARGMATCH_MATCH="${ARGMATCH_MATCH}" ARGMATCH_MATCHFILE="${ARGMATCH_MATCHFILE}" ARGMATCH_STACKLOG="${ARGMATCH_STACKLOG}" ARGMATCH_RECVONLY="${ARGMATCH_RECVONLY}" ARGMATCH_EXACT="${ARGMATCH_EXACT}" ARGMATCH_IGNORECLOSURES="${ARGMATCH_IGNORECLOSURES}" ARGMATCH_IGNORELIBS="${ARGMATCH_IGNORELIBS}" ARGMATCH_IGNORETYPES="${ARGMATCH_IGNORETYPES}" ARGMATCH_DEPTH="${ARGMATCH_DEPTH}" ARGMATCH_MAXELEMS="${ARGMATCH_MAXELEMS}" ARGMATCH_MAXFIELDS="${ARGMATCH_MAXFIELDS}" ARGMATCH_MAXTIME="${ARGMATCH_MAXTIME}" ARGMATCH_MAXMEM="${ARGMATCH_MAXMEM}" ARGMATCH_MAXCALLS="${ARGMATCH_MAXCALLS}" ARGMATCH_CALLINTERVAL="${ARGMATCH_CALLINTERVAL}" ARGMATCH_OUT="${ARGMATCH_OUT}" ARGMATCH_STATS="${ARGMATCH_STATS}" \
    GOFLAGS=-toolexec=/dump_ancestors/argmatch/argmatch CGO_ENABLED=0 GOTOOLCHAIN=local \
    go build -ldflags "-X github.com/prometheus/common/version.Version=0.27.0 -X github.com/prometheus/common/version.Branch=HEAD" \
      -o /alertmanager ./cmd/alertmanager

# The stock image, with this binary in place of its own
FROM ${STOCK}
COPY --from=build /alertmanager /bin/alertmanager
# Include up to 100 creating goroutines in tracebacks
ENV GODEBUG=tracebackancestors=100
