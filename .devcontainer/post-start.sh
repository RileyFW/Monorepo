#!/bin/bash
cd /workspaces/Monorepo/apps/frontend || echo 'folder not found!'
npm i
 
minikube config set cpus 3
minikube config set memory 7800
minikube delete

# wait for docker daemon to start
while ! docker info &> /dev/null; do
    sleep 1
done
# create the cluster
# --minikube-start-flags=--cni=calico installs the Calico CNI, which is required
# for Kubernetes NetworkPolicy enforcement (the runner egress lockdown in
# kubernetes_init/tilt/network-policy-runner.yaml). minikube's default CNI does
# not enforce NetworkPolicy, so without this the policy is accepted but ignored.
#
# --minikube-container-runtime=containerd runs the node on containerd, which is a
# prerequisite for the gVisor (runsc) sandbox runtime enabled just below. Image
# builds still work: Tilt pushes to the ctlptl in-cluster registry, which
# containerd pulls from.
ctlptl create cluster minikube --registry=ctlptl-registry --minikube-container-runtime=containerd --minikube-start-flags=--cni=calico

# Install the gVisor (runsc) sandbox runtime so runner pods can opt into stronger
# syscall isolation via runtimeClassName: gvisor (see
# kubernetes_init/tilt/runtime-class-gvisor.yaml). gVisor is only applied to
# runner pods when the backend is configured with RUNNER_RUNTIME_CLASS=gvisor
# (commented out in kubernetes_init/tilt/deployment-backend.yaml).
#
# NOTE: we install runsc manually rather than via `minikube addons enable gvisor`
# because that addon's installer image (gcr.io/k8s-minikube/gvisor-addon) has been
# removed upstream and no longer pulls. The steps below run on the minikube node.
#
# The ignore-cgroups workaround is required in this dev environment: minikube's
# docker driver nested inside WSL2 produces a doubly-nested cgroup path
# (/docker/.../docker/...) that runsc cannot resolve, failing container creation
# with "Rel: can't make . relative to ...". Ignoring cgroups sidesteps that; the
# sandbox is still bounded by the pod cgroup that containerd/kubelet apply, so the
# memory limit in job-runner.yaml is still enforced. A native (non-nested)
# containerd node would not need this flag.
minikube ssh -- 'set -e
  ARCH=$(uname -m)
  URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}
  sudo curl -fsSL -o /usr/local/bin/runsc ${URL}/runsc
  sudo curl -fsSL -o /usr/local/bin/containerd-shim-runsc-v1 ${URL}/containerd-shim-runsc-v1
  sudo chmod a+rx /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1
  sudo tee /etc/containerd/runsc.toml >/dev/null <<CFG
[runsc_config]
  ignore-cgroups = "true"
CFG
  sudo tee -a /etc/containerd/config.toml >/dev/null <<CFG

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
    TypeUrl = "io.containerd.runsc.v1.options"
    ConfigPath = "/etc/containerd/runsc.toml"
CFG
  sudo systemctl restart containerd'

# Install some pip packages
pip install flask-cors
pip install kubernetes
pip install pymongo
pip install bson
pip install pyyaml
pip install gunicorn
pip install numpy
pip install configparser
pip install python-magic
pip install python-dotenv
pip install matplotlib
pip install pydantic
pip install pipreqs