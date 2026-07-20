# In-cluster registry (DEV / minikube)

Phase 0 of the runner registry / base-image split. This directory documents the
private, in-cluster container registry that the dev stack uses so that a future
Kaniko build Job can push runner images and runner Jobs can pull them — all
without touching public Docker Hub.

## Decision: use the existing ctlptl registry

The dev cluster **already ships an in-cluster registry**, and it is the cleanest
possible integration with the existing Tilt flow, so Phase 0 adopts it rather
than introducing a second registry.

`.devcontainer/post-start.sh` creates the cluster with:

```bash
ctlptl create cluster minikube --registry=ctlptl-registry
```

[ctlptl](https://github.com/tilt-dev/ctlptl) (a Tilt-team tool that is already
installed and used in this repo) does three things for us:

1. Runs a `registry:2` container named `ctlptl-registry` and joins it to the
   `minikube` docker network, so the cluster node can reach it.
2. Publishes a `local-registry-hosting` ConfigMap in the `kube-public`
   namespace — the [KEP-1755](https://github.com/kubernetes/enhancements/tree/master/keps/sig-cluster-lifecycle/generic/1755-communicating-a-local-registry)
   discovery standard:

   ```yaml
   data:
     localRegistryHosting.v1: |
       host: localhost:<random-port>       # push target from the dev host / Tilt
       hostFromClusterNetwork: ctlptl-registry:5000   # pull target for in-cluster pods
       hostFromContainerRuntime: ctlptl-registry:5000
   ```

3. Because Tilt reads that ConfigMap automatically, **`docker_build` pushes are
   already redirected to this registry with no `default_registry` line needed**,
   and image refs in the k8s manifests are rewritten to the in-cluster address.

### Why not the alternatives?

- **`minikube addons enable registry`** — spins up a *separate* registry plus an
  `HTTPS`/proxy shim, and it does **not** publish the `local-registry-hosting`
  ConfigMap. Tilt would keep pushing to the ctlptl registry, so we'd have two
  registries fighting and manual `default_registry` wiring to reconcile them.
- **A hand-rolled `Deployment` + `Service` (+ `PVC`) here** — same problem: Tilt
  auto-detects the ctlptl registry, so a second registry would sit unused unless
  we override discovery, and it would duplicate infrastructure ctlptl already
  manages (network joining, insecure-registry trust on the node, teardown).

The ctlptl registry is the Tilt-native path and is already wired end to end, so
Phase 0 simply makes the decision explicit (this doc + `registry.yaml`) instead
of adding redundant infrastructure.

## Files

- `registry.yaml` — declarative ctlptl `Registry` resource that reproduces what
  `post-start.sh` creates imperatively. Apply/reconcile with
  `ctlptl apply -f kubernetes_init/registry/registry.yaml` (idempotent).

## Verify it is reachable in-cluster

```bash
# 1. The discovery ConfigMap exists and points pods at ctlptl-registry:5000
kubectl get configmap -n kube-public local-registry-hosting \
  -o jsonpath='{.data.localRegistryHosting\.v1}'

# 2. The registry container is on the minikube network (node can resolve it)
docker network inspect minikube \
  --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}'

# 3. ctlptl agrees the registry is running
ctlptl get registry ctlptl-registry

# 4. End-to-end push + in-cluster pull smoke test
HOSTPORT=$(kubectl get configmap -n kube-public local-registry-hosting \
  -o jsonpath='{.data.localRegistryHosting\.v1}' | awk '/host:/{print $2; exit}')
docker pull busybox:latest
docker tag busybox:latest "$HOSTPORT/busybox:smoke"
docker push "$HOSTPORT/busybox:smoke"
# Pod pulls via the in-cluster name, proving cluster-side reachability:
kubectl run registry-smoke --rm -it --restart=Never \
  --image=ctlptl-registry:5000/busybox:smoke -- echo ok
```

## Follow-ups for Phases 1–2

- The registry is plain-HTTP (insecure). The minikube node already trusts it
  (ctlptl configured that), but a Kaniko build Job pushing to it will need
  `--insecure` / `--insecure-registry=ctlptl-registry:5000`.
- The Kaniko Job should build `FROM ctlptl-registry:5000/runner-base` (the base
  image added in Phase 0 — see `apps/runner/runner-base.Dockerfile`) and push
  the per-experiment image back to `ctlptl-registry:5000`.
- The runner Job image ref (`apps/backend/job-runner.yaml` /
  `IMAGE_RUNNER`) will then point at `ctlptl-registry:5000/...` instead of the
  public `gladospipeline/glados-runner:main`.
