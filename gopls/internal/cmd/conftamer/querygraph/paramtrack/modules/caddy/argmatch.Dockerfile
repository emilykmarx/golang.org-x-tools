# Caddy's ingress controller (caddyserver-ingress), built under argmatch and contexttrack, like
# prometheus/prombench-argmatch.Dockerfile (whose comments describe the ARGMATCH_* args). Build from the parent of
# the checkouts ($CT), as paramtrack's testbeds.py does:
#   docker build -f <this file> -t caddy-ingress-conftamer:latest .
ARG BUILDER=prometheus-builder:conftamer

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
# Patch Go's net/http (on top of the builder's own Go patch) to log HTTP messages, and Caddy's caddyhttp routes to
# log their routing; see conftamer/contexttrack/README.md. Nothing is logged unless CONFTAMER_EVENTS is set.
COPY conftamer/contexttrack/go-inlibrary.patch conftamer/contexttrack/caddy-route.patch /patches/
RUN cd /usr/local/go && patch -p4 < /patches/go-inlibrary.patch
COPY golang.org-x-tools/gopls/conftamer/stacklog/ /golang.org-x-tools/gopls/conftamer/stacklog/
COPY dump_ancestors/argmatch/ /dump_ancestors/argmatch/
RUN cd /dump_ancestors/argmatch && go build -o argmatch .
COPY caddyserver-ingress/ /src/
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    cd /src && \
    go mod download github.com/caddyserver/caddy/v2 && \
    caddy=$(go list -m -f '{{.Dir}}' github.com/caddyserver/caddy/v2) && \
    rm -rf /caddy-conftamer && cp -r "$caddy" /caddy-conftamer && chmod -R u+w /caddy-conftamer && \
    (cd /caddy-conftamer && patch -p1 < /patches/caddy-route.patch) && \
    go mod edit -replace=github.com/caddyserver/caddy/v2=/caddy-conftamer && \
    ARGMATCH_FUNC="${ARGMATCH_FUNC}" ARGMATCH_MATCH="${ARGMATCH_MATCH}" ARGMATCH_MATCHFILE="${ARGMATCH_MATCHFILE}" ARGMATCH_STACKLOG="${ARGMATCH_STACKLOG}" ARGMATCH_RECVONLY="${ARGMATCH_RECVONLY}" ARGMATCH_EXACT="${ARGMATCH_EXACT}" ARGMATCH_IGNORECLOSURES="${ARGMATCH_IGNORECLOSURES}" ARGMATCH_IGNORELIBS="${ARGMATCH_IGNORELIBS}" ARGMATCH_IGNORETYPES="${ARGMATCH_IGNORETYPES}" ARGMATCH_DEPTH="${ARGMATCH_DEPTH}" ARGMATCH_MAXELEMS="${ARGMATCH_MAXELEMS}" ARGMATCH_MAXFIELDS="${ARGMATCH_MAXFIELDS}" ARGMATCH_MAXTIME="${ARGMATCH_MAXTIME}" ARGMATCH_MAXMEM="${ARGMATCH_MAXMEM}" ARGMATCH_MAXCALLS="${ARGMATCH_MAXCALLS}" ARGMATCH_CALLINTERVAL="${ARGMATCH_CALLINTERVAL}" ARGMATCH_OUT="${ARGMATCH_OUT}" ARGMATCH_STATS="${ARGMATCH_STATS}" \
    GOFLAGS=-toolexec=/dump_ancestors/argmatch/argmatch CGO_ENABLED=0 \
    go build -o /ingress-controller ./cmd/caddy

# As the controller's own Dockerfile
FROM alpine:latest
RUN apk --no-cache add ca-certificates
EXPOSE 80 443
COPY --from=build /ingress-controller /ingress-controller
ENTRYPOINT ["/ingress-controller"]
# Include up to 100 creating goroutines in tracebacks
ENV GODEBUG=tracebackancestors=100
