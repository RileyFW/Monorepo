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
ctlptl create cluster minikube --registry=ctlptl-registry --minikube-start-flags=--cni=calico

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