# The Kubernetes API server (kube-apiserver), built under argmatch and contexttrack, like
# prometheus/prombench-argmatch.Dockerfile (whose comments describe the ARGMATCH_* args). Build from the parent of the
# checkouts ($CT), with the Kubernetes checkout (module.py's MODULE_DIR, outside $CT) as the build context
# `kubernetes`, as paramtrack's testbeds.py does:
#   docker build -f <this file> --build-context kubernetes=<checkout> -t kube-apiserver-conftamer:v1.31.1 .
ARG BUILDER=prometheus-builder:conftamer
# The image kind's node runs (testbeds/prombench/upstream.py's NODE_IMAGE), whose binary this replaces
ARG STOCK=registry.k8s.io/kube-apiserver:v1.31.1

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
# Patch Go's net/http (on top of the builder's own Go patch) to log HTTP messages, and Kubernetes' copies of its
# routers (k8s.io/apiserver's PathRecorderMux, go-restful), gRPC (its client: etcd calls) and x/net's HTTP/2 server
# (which serves the API over HTTP/2) to log theirs; see conftamer/contexttrack/README.md. Nothing is logged unless
# CONFTAMER_EVENTS is set.
COPY conftamer/contexttrack/go-inlibrary.patch conftamer/contexttrack/k8s-pathrecorder-route.patch \
     conftamer/contexttrack/go-restful-route.patch conftamer/contexttrack/grpc-route.patch \
     conftamer/contexttrack/grpc-client.patch conftamer/contexttrack/x-net-http2-server.sh /patches/
RUN cd /usr/local/go && patch -p4 < /patches/go-inlibrary.patch
COPY golang.org-x-tools/gopls/conftamer/stacklog/ /golang.org-x-tools/gopls/conftamer/stacklog/
COPY dump_ancestors/argmatch/ /dump_ancestors/argmatch/
RUN cd /dump_ancestors/argmatch && go build -o argmatch .
COPY --from=kubernetes . /src/
# go.work uses the staging modules (k8s.io/apiserver, ...) in place, and vendor/ for the rest. The version is the
# checkout's (its git metadata isn't read here)
RUN --mount=type=cache,target=/root/.cache/go-build \
    cd /src && \
    (cd staging/src/k8s.io/apiserver && patch -p1 < /patches/k8s-pathrecorder-route.patch) && \
    (cd vendor/github.com/emicklei/go-restful/v3 && patch -p1 < /patches/go-restful-route.patch) && \
    (cd vendor/google.golang.org/grpc && patch -p1 < /patches/grpc-route.patch && \
     patch -p1 < /patches/grpc-client.patch) && \
    sh /patches/x-net-http2-server.sh vendor/golang.org/x/net && \
    ARGMATCH_FUNC="${ARGMATCH_FUNC}" ARGMATCH_MATCH="${ARGMATCH_MATCH}" ARGMATCH_MATCHFILE="${ARGMATCH_MATCHFILE}" ARGMATCH_STACKLOG="${ARGMATCH_STACKLOG}" ARGMATCH_RECVONLY="${ARGMATCH_RECVONLY}" ARGMATCH_EXACT="${ARGMATCH_EXACT}" ARGMATCH_IGNORECLOSURES="${ARGMATCH_IGNORECLOSURES}" ARGMATCH_IGNORELIBS="${ARGMATCH_IGNORELIBS}" ARGMATCH_IGNORETYPES="${ARGMATCH_IGNORETYPES}" ARGMATCH_DEPTH="${ARGMATCH_DEPTH}" ARGMATCH_MAXELEMS="${ARGMATCH_MAXELEMS}" ARGMATCH_MAXFIELDS="${ARGMATCH_MAXFIELDS}" ARGMATCH_MAXTIME="${ARGMATCH_MAXTIME}" ARGMATCH_MAXMEM="${ARGMATCH_MAXMEM}" ARGMATCH_MAXCALLS="${ARGMATCH_MAXCALLS}" ARGMATCH_CALLINTERVAL="${ARGMATCH_CALLINTERVAL}" ARGMATCH_OUT="${ARGMATCH_OUT}" ARGMATCH_STATS="${ARGMATCH_STATS}" \
    GOFLAGS=-toolexec=/dump_ancestors/argmatch/argmatch CGO_ENABLED=0 GOTOOLCHAIN=local \
    go build -ldflags "-X k8s.io/component-base/version.gitVersion=v1.31.1 -X k8s.io/component-base/version.gitMajor=1 -X k8s.io/component-base/version.gitMinor=31" \
      -o /kube-apiserver ./cmd/kube-apiserver

# The stock image, with this binary in place of its own
FROM ${STOCK}
COPY --from=build /kube-apiserver /usr/local/bin/kube-apiserver
# Include up to 100 creating goroutines in tracebacks
ENV GODEBUG=tracebackancestors=100
