# Grafana, built under argmatch and contexttrack, like prometheus/prombench-argmatch.Dockerfile (whose comments describe
# the ARGMATCH_* args). Build from the parent of the checkouts ($CT), as paramtrack's testbeds.py does:
#   docker build -f <this file> -t grafana-conftamer:13.1.0 .
# The checkout (grafana-paramtrack) is upstream's v13.1.0, the version contexttrack's Grafana patches were made
# against; the testbed runs the same version stock (testbeds.py's Grafana; prombench itself ships 11.0.0).
ARG BUILDER=prometheus-builder:go1.26.4  # (testbeds/prombench/upstream.py's BUILDER_TAG)
ARG STOCK=grafana/grafana:13.1.0

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
# Patch Go's net/http to log HTTP messages, and Grafana's routers to log their
# routes: its pkg/web router (in place), and, as patched copies that workspace replaces point at, gorilla/mux and the
# embedded Kubernetes-style API server's (k8s.io/apiserver's PathRecorderMux, go-restful: the /apis/... endpoints);
# see conftamer/contexttrack/README.md. Nothing is logged unless CONFTAMER_EVENTS is set. (Not gRPC: grpc-client.patch
# doesn't apply to Grafana's grpc v1.81.1, and prombench's traffic to Grafana is HTTP.)
COPY conftamer/contexttrack/go-inlibrary.patch conftamer/contexttrack/grafana-web-route.patch \
     conftamer/contexttrack/gorilla-mux-route.patch conftamer/contexttrack/k8s-pathrecorder-route.patch \
     conftamer/contexttrack/go-restful-route.patch /patches/
RUN cd /usr/local/go && patch -p4 < /patches/go-inlibrary.patch
COPY golang.org-x-tools/gopls/conftamer/stacklog/ /golang.org-x-tools/gopls/conftamer/stacklog/
COPY dump_ancestors/argmatch/ /dump_ancestors/argmatch/
RUN cd /dump_ancestors/argmatch && go build -o argmatch .
COPY grafana-paramtrack/ /src/
# Wire's graph (pkg/server/wire_gen.go) and the themes are generated in the checkout, so a plain go build does what
# `make build-go` does (oss build tags)
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    cd /src && \
    patch -p1 < /patches/grafana-web-route.patch && \
    for mp in github.com/gorilla/mux:gorilla-mux-route.patch k8s.io/apiserver:k8s-pathrecorder-route.patch \
              github.com/emicklei/go-restful/v3:go-restful-route.patch; do \
      m=${mp%%:*} && p=${mp##*:} && copy=/conftamer-mods/$m && \
      GOWORK=off go mod download $m && dir=$(GOWORK=off go list -m -f '{{.Dir}}' $m) && \
      rm -rf $copy && mkdir -p $(dirname $copy) && cp -r "$dir" $copy && chmod -R u+w $copy && \
      (cd $copy && patch -p1 < /patches/$p) && \
      go work edit -replace=$m=$copy || exit 1; \
    done && \
    ARGMATCH_FUNC="${ARGMATCH_FUNC}" ARGMATCH_MATCH="${ARGMATCH_MATCH}" ARGMATCH_MATCHFILE="${ARGMATCH_MATCHFILE}" ARGMATCH_STACKLOG="${ARGMATCH_STACKLOG}" ARGMATCH_RECVONLY="${ARGMATCH_RECVONLY}" ARGMATCH_EXACT="${ARGMATCH_EXACT}" ARGMATCH_IGNORECLOSURES="${ARGMATCH_IGNORECLOSURES}" ARGMATCH_IGNORELIBS="${ARGMATCH_IGNORELIBS}" ARGMATCH_IGNORETYPES="${ARGMATCH_IGNORETYPES}" ARGMATCH_DEPTH="${ARGMATCH_DEPTH}" ARGMATCH_MAXELEMS="${ARGMATCH_MAXELEMS}" ARGMATCH_MAXFIELDS="${ARGMATCH_MAXFIELDS}" ARGMATCH_MAXTIME="${ARGMATCH_MAXTIME}" ARGMATCH_MAXMEM="${ARGMATCH_MAXMEM}" ARGMATCH_MAXCALLS="${ARGMATCH_MAXCALLS}" ARGMATCH_CALLINTERVAL="${ARGMATCH_CALLINTERVAL}" ARGMATCH_OUT="${ARGMATCH_OUT}" ARGMATCH_STATS="${ARGMATCH_STATS}" \
    GOFLAGS=-toolexec=/dump_ancestors/argmatch/argmatch CGO_ENABLED=0 GOTOOLCHAIN=local \
    go build -buildvcs=false -tags oss -ldflags "-X main.version=13.1.0 -X main.commit=paramtrack -X main.buildBranch=HEAD" \
      -o /grafana ./pkg/cmd/grafana

# The stock image, with this binary in place of its own
FROM ${STOCK}
COPY --from=build /grafana /usr/share/grafana/bin/grafana
# Include up to 100 creating goroutines in tracebacks
ENV GODEBUG=tracebackancestors=100
