#!/bin/bash -e

# Sets up a fresh Ubuntu VM for paramtrack's prombench testbed (../../testbeds.py, upstream.py). Bootstrap:
#   mkdir -p /home/emily/projects/config_tracing && cd /home/emily/projects/config_tracing &&
#   git clone https://github.com/emilykmarx/golang.org-x-tools.git &&
#   bash golang.org-x-tools/gopls/internal/cmd/conftamer/querygraph/paramtrack/testbeds/prombench/vm_setup.sh
# Then log out and back in. upstream.py clones upstream prometheus/test-infra and builds its infra tool itself.

set -x

# 1. DEPS
GO_VERSION=1.26.2
KIND_VERSION=v0.33.0
KUBECTL_VERSION=v1.31.13

ARCH=$(dpkg --print-architecture) # amd64 or arm64

sudo apt-get update
sudo apt-get install -y curl git ca-certificates

# Go
curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${ARCH}.tar.gz" -o /tmp/go.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf /tmp/go.tar.gz
rm /tmp/go.tar.gz
if ! grep -q '/usr/local/go/bin' ~/.profile; then
    echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> ~/.profile
fi
export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin
go version

# kind
curl -fsSL "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}" -o /tmp/kind
sudo install -m 0755 /tmp/kind /usr/local/bin/kind
rm /tmp/kind
kind version

# Docker Engine (https://docs.docker.com/engine/install/ubuntu/)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
# Run docker without sudo (takes effect on next login)
sudo usermod -aG docker "$USER"

# kubectl
curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" -o /tmp/kubectl
sudo install -m 0755 /tmp/kubectl /usr/local/bin/kubectl
rm /tmp/kubectl
kubectl version --client


# Playwright with headless Chromium, used by dump_ancestors/argmatch/experiment
# (view_dashboards.py) to load Grafana dashboards like a person viewing them
sudo apt-get install -y python3-pip
pip3 install --user playwright
python3 -m playwright install --with-deps chromium

# 2. REPOS
cd /home/emily/projects/config_tracing
git clone https://github.com/emilykmarx/prometheus.git
pushd prometheus; git checkout prombench; popd
git clone https://github.com/emilykmarx/argmatch.git dump_ancestors
# Caddy's ingress controller, for the prombench-caddyICingress-caddyIC testbed
git clone https://github.com/emilykmarx/caddyserver-ingress.git
git clone https://github.com/emilykmarx/conftamer
pushd conftamer; git checkout paramtrack-routes; popd  # (contexttrack patches: go-openapi route, received path in "Response sent"; until merged)
# The Kubernetes API server (modules/kube-apiserver/module.py's MODULE_DIR, outside this directory): v1.31.1, as
# upstream.py's NODE_IMAGE (blobs fetched on demand: the history is large)
git clone --filter=blob:none --single-branch --branch conftamer-ancestry https://github.com/emilykmarx/kubernetes.git \
    /home/emily/go/src/k8s.io/kubernetes
# Alertmanager and Grafana (modules/alertmanager/, modules/grafana/): upstream's, at the versions their module.py's
# MODULE_DIR comments give
git -c advice.detachedHead=false clone --depth 1 --branch v0.27.0 https://github.com/prometheus/alertmanager.git \
    alertmanager-paramtrack
git -c advice.detachedHead=false clone --depth 1 --branch v13.1.0 https://github.com/grafana/grafana.git \
    grafana-paramtrack

# kind: kube-proxy and promtail need more inotify instances than the default 128 (upstream.py also sets it, but
# only until the next reboot)
echo "fs.inotify.max_user_instances = 512" | sudo tee /etc/sysctl.d/99-prombench-inotify.conf
sudo sysctl --system

# Mounted into prometheus-meta (manifests/cluster_kind.yaml); must exist before the cluster is created
mkdir -p dump_ancestors/argmatch-out

echo 'Log out and back in for docker group membership to take effect, and to source ~/.profile'
