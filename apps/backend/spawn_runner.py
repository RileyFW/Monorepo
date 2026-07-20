"""Module that provides functionality to create a job for the runner"""

import os
import time
import sys
import yaml
import json
from kubernetes import client, config
config.load_incluster_config()
batch_v1 = client.BatchV1Api()
RUNNER_PATH = "./job-runner.yaml"

def create_job_object(experiment_data, image_override=None):
    """Function that creates the job object for the runner.

    If image_override is provided (a per-experiment image built with the user's
    declared dependencies, see build_image.py), the runner Job runs from it.
    Otherwise the behaviour is unchanged: the IMAGE_RUNNER env image is used if
    set, else the default image baked into job-runner.yaml.
    """
    # Configure Pod template container
    job_name = "runner-" + experiment_data['experiment']['id']

    # Absolute path: the runner Job sets workingDir to a writable volume (/work),
    # so the script must be referenced by its baked-in location under /app.
    job_command = ["python3", "/app/runner.py", json.dumps(experiment_data)]

    runner_body = get_yaml_file_body(RUNNER_PATH)

    runner_body['metadata']['name'] = job_name
    pod_spec = runner_body['spec']['template']['spec']
    pod_spec['containers'][0]['command'] = job_command

    # Spread the experiment's trials across `workers` runner pods. A single
    # Indexed Job owns every shard: k8s injects JOB_COMPLETION_INDEX into each
    # pod (0..workers-1), which the runner reads to pick its trial subset. Using
    # one Indexed Job (rather than N separate Jobs) keeps the job name
    # runner-<expId>, so cancel and pod/log lookup are unchanged.
    workers = experiment_data['experiment'].get('workers') or 1
    try:
        workers = max(1, int(workers))
    except (TypeError, ValueError):
        workers = 1
    runner_body['spec']['completionMode'] = 'Indexed'
    runner_body['spec']['completions'] = workers
    runner_body['spec']['parallelism'] = workers

    # Config-gated gVisor sandboxing: only set runtimeClassName when the backend
    # is told to (RUNNER_RUNTIME_CLASS, e.g. "gvisor"). This keeps the runner
    # runnable on clusters where the sandbox runtime isn't installed -- pods that
    # request a missing RuntimeClass are rejected -- so gVisor is strictly opt-in.
    runtime_class = os.getenv("RUNNER_RUNTIME_CLASS")
    if runtime_class:
        pod_spec['runtimeClassName'] = runtime_class

    if image_override:
        pod_spec['containers'][0]['image'] = image_override
    elif os.getenv("IMAGE_RUNNER"):
        # Get the image name
        image_name = str(os.getenv("IMAGE_RUNNER"))
        pod_spec['containers'][0]['image'] = image_name

    return runner_body

def create_finalize_job_object(experiment_id):
    """Create the one-shot finalize Job for a sharded experiment.

    Spawned by the backend once every runner-pod shard has reported complete.
    Runs the same runner image in --finalize mode: it pulls each shard's partial
    results/artifacts, merges them into the single results.csv/zip/plot, sends the
    completion email, and writes the terminal finished/status fields. It is a
    plain single-pod Job (no Indexed parallelism) named runner-<expId>-finalize.
    """
    job_name = "runner-" + str(experiment_id) + "-finalize"
    payload = {"experiment": {"id": experiment_id}}
    job_command = ["python3", "/app/runner.py", "--finalize", json.dumps(payload)]

    runner_body = get_yaml_file_body(RUNNER_PATH)

    runner_body['metadata']['name'] = job_name
    pod_spec = runner_body['spec']['template']['spec']
    pod_spec['containers'][0]['command'] = job_command

    # Match the runner image selection used for the shard pods so the finalize
    # step runs the same code/deps.
    runtime_class = os.getenv("RUNNER_RUNTIME_CLASS")
    if runtime_class:
        pod_spec['runtimeClassName'] = runtime_class
    if os.getenv("IMAGE_RUNNER"):
        pod_spec['containers'][0]['image'] = str(os.getenv("IMAGE_RUNNER"))

    return runner_body

def create_job(api_instance, job):
    """Function that creates the job for the runner"""
    api_instance.create_namespaced_job(
        body=job,
        namespace="default")

def main(experiment_id: int):
    """Function that gets called when the file is ran"""
    runner = create_job_object(experiment_id)
    create_job(batch_v1, runner)

def get_yaml_file_body(file_path):
    """Function to get yaml file from body"""
    body = None
    with open(file_path, encoding="utf-8") as yaml_file:
        body = yaml.safe_load(yaml_file)
    return body

if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise ValueError("Error: Too few arguments. Needs ID (ex: python spawn_runner.py 1234)")
    elif len(sys.argv) > 2:
        raise ValueError("Error: Too many arguments. Needs ID (ex: python spawn_runner.py 1234)")
    main(sys.argv[1])
