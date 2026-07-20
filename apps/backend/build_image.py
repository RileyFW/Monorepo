"""Phase 2: per-experiment image build orchestration.

When an experiment declares dependencies, the backend builds a small image
FROM the runner base image with those pip/apt dependencies layered on top, via
an in-cluster Kaniko Job, and pushes it to the in-cluster registry. The backend
then spawns the runner Job FROM the built image (see spawn_runner.py).

Key design points:
- **Backward compatible:** ``build_experiment_image`` returns ``None`` when the
  experiment declares no dependencies, so the caller falls back to the existing
  ``IMAGE_RUNNER`` image and behaviour is unchanged for current experiments.
- **Cached by content:** the built image tag is a hash of the base image plus
  the rendered build context, so identical dependency sets are built once and
  reused (the registry is checked before building).

Input contract (populated by the frontend in Phase 1, read defensively here):
  experiment_data['experiment']['pipRequirements'] : str  -> requirements.txt contents
  experiment_data['experiment']['aptPackages']     : list[str] | str -> apt packages
"""
import hashlib
import os
import time

import requests
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ---- Configuration (env-overridable; defaults target the dev/minikube registry) ----
REGISTRY_HOST = os.getenv("REGISTRY_HOST", "ctlptl-registry:5000")
RUNNER_BASE_IMAGE = os.getenv("RUNNER_BASE_IMAGE", f"{REGISTRY_HOST}/runner-base:latest")
# The dev ctlptl registry serves plain HTTP; production (TLS) should set this false.
REGISTRY_INSECURE = os.getenv("REGISTRY_INSECURE", "true").lower() in ("1", "true", "yes")
EXP_IMAGE_REPO = os.getenv("EXP_IMAGE_REPO", "glados-exp")
BUILD_NAMESPACE = os.getenv("BUILD_NAMESPACE", "default")
KANIKO_BUILD_TIMEOUT_SECONDS = int(os.getenv("KANIKO_BUILD_TIMEOUT_SECONDS", "900"))

BUILDER_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "job-builder.yaml")


def _normalize_apt_packages(apt_packages):
    """Accept a list or a whitespace/newline-separated string, return a clean list."""
    if not apt_packages:
        return []
    if isinstance(apt_packages, str):
        return [pkg for pkg in apt_packages.split() if pkg]
    return [str(pkg).strip() for pkg in apt_packages if str(pkg).strip()]


def render_dockerfile(pip_requirements, apt_packages):
    """Render the build Dockerfile from the declared dependencies.

    Returns ``(dockerfile_text, context_files)`` where context_files is a dict of
    filename -> contents that must be present in the build context.
    """
    apt_list = _normalize_apt_packages(apt_packages)
    lines = [f"FROM {RUNNER_BASE_IMAGE}"]
    context_files = {}

    if apt_list:
        context_files["apt-packages.txt"] = "\n".join(apt_list) + "\n"
        lines += [
            "COPY apt-packages.txt /tmp/apt-packages.txt",
            "RUN apt-get update && "
            "xargs -a /tmp/apt-packages.txt apt-get install -y --no-install-recommends && "
            "rm -rf /var/lib/apt/lists/*",
        ]

    if pip_requirements and pip_requirements.strip():
        context_files["requirements.txt"] = pip_requirements
        lines += [
            "COPY requirements.txt /tmp/requirements.txt",
            "RUN pip install --no-cache-dir -r /tmp/requirements.txt",
        ]

    dockerfile_text = "\n".join(lines) + "\n"
    context_files["Dockerfile"] = dockerfile_text
    return dockerfile_text, context_files


def _content_tag(context_files):
    """Deterministic image tag from the base image + full build context."""
    digest = hashlib.sha256()
    digest.update(RUNNER_BASE_IMAGE.encode("utf-8"))
    for name in sorted(context_files):
        digest.update(b"\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(context_files[name].encode("utf-8"))
    return digest.hexdigest()[:16]


def image_exists(tag):
    """Return True if EXP_IMAGE_REPO:tag already exists in the registry."""
    scheme = "http" if REGISTRY_INSECURE else "https"
    url = f"{scheme}://{REGISTRY_HOST}/v2/{EXP_IMAGE_REPO}/manifests/{tag}"
    headers = {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json, "
                  "application/vnd.oci.image.manifest.v1+json"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        # Treat an unreachable registry as "not cached"; the build will surface the real error.
        return False


def _kaniko_args(destination):
    args = [
        "--dockerfile=/workspace/Dockerfile",
        "--context=dir:///workspace",
        f"--destination={destination}",
        "--verbosity=info",
    ]
    if REGISTRY_INSECURE:
        args += ["--insecure", "--insecure-pull", "--skip-tls-verify", "--skip-tls-verify-pull"]
    return args


def _build_job_body(job_name, configmap_name, destination):
    with open(BUILDER_TEMPLATE_PATH, encoding="utf-8") as template_file:
        body = yaml.safe_load(template_file)
    body["metadata"]["name"] = job_name
    spec = body["spec"]["template"]["spec"]
    spec["containers"][0]["args"] = _kaniko_args(destination)
    for volume in spec["volumes"]:
        if volume["name"] == "cm":
            volume["configMap"]["name"] = configmap_name
    return body


def _delete_configmap(core_api, name):
    try:
        core_api.delete_namespaced_config_map(name, BUILD_NAMESPACE)
    except ApiException as err:
        if err.status != 404:
            raise


def _delete_job(batch_api, name):
    try:
        batch_api.delete_namespaced_job(name, BUILD_NAMESPACE, propagation_policy="Background")
    except ApiException as err:
        if err.status != 404:
            raise


def _wait_for_job(batch_api, job_name, timeout_seconds):
    """Poll the build Job until it completes or fails. Returns 'Complete'/'Failed'/'Timeout'."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        # read_namespaced_job (not ..._status): the full Job carries .status and only
        # needs the "jobs" get permission, avoiding the separate jobs/status subresource.
        status = batch_api.read_namespaced_job(job_name, BUILD_NAMESPACE).status
        for condition in (status.conditions or []):
            if condition.type == "Complete" and condition.status == "True":
                return "Complete"
            if condition.type == "Failed" and condition.status == "True":
                return "Failed"
        time.sleep(3)
    return "Timeout"


def _kaniko_logs(core_api, job_name):
    """Best-effort fetch of the kaniko container logs for diagnostics."""
    try:
        pods = core_api.list_namespaced_pod(
            BUILD_NAMESPACE, label_selector=f"job-name={job_name}"
        )
        if not pods.items:
            return "(no build pod found)"
        pod_name = pods.items[0].metadata.name
        logs = core_api.read_namespaced_pod_log(pod_name, BUILD_NAMESPACE, container="kaniko")
        return "\n".join(logs.splitlines()[-20:])
    except ApiException:
        return "(could not read build logs)"


def build_experiment_image(experiment_data, batch_api, core_api):
    """Build (or reuse) the per-experiment image and return its full reference.

    Returns ``None`` when the experiment declares no dependencies, signalling the
    caller to use the default runner image (unchanged current behaviour).
    """
    experiment = experiment_data.get("experiment", {})
    pip_requirements = experiment.get("pipRequirements")
    apt_packages = experiment.get("aptPackages")

    if not (pip_requirements and pip_requirements.strip()) and not _normalize_apt_packages(apt_packages):
        # No declared dependencies -> nothing to build; use the default runner image.
        return None

    _, context_files = render_dockerfile(pip_requirements, apt_packages)
    tag = _content_tag(context_files)
    destination = f"{REGISTRY_HOST}/{EXP_IMAGE_REPO}:{tag}"

    # Cache hit: identical dependency set already built.
    if image_exists(tag):
        return destination

    exp_id = experiment.get("id", tag)
    job_name = f"builder-{exp_id}"
    configmap_name = f"builder-ctx-{exp_id}"

    # Clean any leftovers from a previous attempt for this experiment.
    _delete_job(batch_api, job_name)
    _delete_configmap(core_api, configmap_name)

    configmap = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=configmap_name),
        data=context_files,
    )
    core_api.create_namespaced_config_map(BUILD_NAMESPACE, configmap)
    batch_api.create_namespaced_job(
        BUILD_NAMESPACE, _build_job_body(job_name, configmap_name, destination)
    )

    try:
        result = _wait_for_job(batch_api, job_name, KANIKO_BUILD_TIMEOUT_SECONDS)
        if result != "Complete":
            logs = _kaniko_logs(core_api, job_name)
            raise RuntimeError(
                f"Image build for experiment {exp_id} did not complete ({result}). "
                f"Kaniko logs (tail):\n{logs}"
            )
        return destination
    finally:
        _delete_configmap(core_api, configmap_name)
        _delete_job(batch_api, job_name)


def _ensure_config():
    """Load in-cluster config when running as a pod, else fall back to local kubeconfig."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


if __name__ == "__main__":
    # Local smoke test against the current kube context (e.g. from the devcontainer):
    #   python build_image.py
    _ensure_config()
    _batch = client.BatchV1Api()
    _core = client.CoreV1Api()
    _data = {"experiment": {"id": "localtest", "pipRequirements": "cowsay==6.1\n"}}
    print("Building test image...")
    print("Built image reference:", build_experiment_image(_data, _batch, _core))
