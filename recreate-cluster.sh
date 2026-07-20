#!/bin/bash
#
# Recreate the local minikube dev cluster from scratch.
#
# This tears down the existing ctlptl/minikube cluster (and the ctlptl-managed
# in-cluster registry that post-start.sh does NOT clean up on its own) and then
# re-runs .devcontainer/post-start.sh, which rebuilds the cluster exactly the way
# the dev container does on start-up (Calico CNI, containerd runtime, gVisor/runsc
# install, and the pip packages).
#
# Use this when your cluster is in a bad state and you want a clean slate without
# rebuilding the whole dev container. Run `tilt down` first if Tilt is up, and
# run `tilt up` again after this finishes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POST_START="${REPO_ROOT}/.devcontainer/post-start.sh"

if [[ ! -f "${POST_START}" ]]; then
    echo "Could not find ${POST_START} - are you running this from the repo?" >&2
    exit 1
fi

echo "==> Tearing down existing cluster..."

# ctlptl owns the cluster + the registry container. Deleting the cluster through
# ctlptl (rather than a bare `minikube delete`) also removes the ctlptl-registry,
# which a plain minikube delete leaves behind and which can wedge the next
# `ctlptl create cluster`. Both deletes are best-effort: a missing cluster/registry
# is fine, so we don't let a nonzero exit abort the script here.
if command -v ctlptl &> /dev/null; then
    ctlptl delete cluster minikube --cascade=true --ignore-not-found || true
    # Fallback in case the registry wasn't "connected" to the cluster (e.g. the
    # cluster was already gone), so --cascade left it behind.
    ctlptl delete registry ctlptl-registry --ignore-not-found || true
fi

# Belt-and-suspenders: make sure no stale minikube profile survives. post-start.sh
# runs `minikube delete` too, but doing it here keeps teardown self-contained.
if command -v minikube &> /dev/null; then
    minikube delete || true
fi

echo "==> Recreating cluster via post-start.sh..."
bash "${POST_START}"

echo "==> Done. The minikube cluster has been recreated."
echo "    Run 'tilt up' from the repo root to redeploy the stack."
