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

# Install the gVisor (runsc) sandbox runtime so runner pods can opt into
# stronger syscall isolation via runtimeClassName: gvisor (see
# kubernetes_init/tilt/runtime-class-gvisor.yaml). gVisor is only applied to
# runner pods when the backend is configured with RUNNER_RUNTIME_CLASS=gvisor
# (commented out in kubernetes_init/tilt/deployment-backend.yaml until confirmed
# working in this environment).
minikube addons enable gvisor

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