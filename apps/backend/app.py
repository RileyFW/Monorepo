"""Module that uses flask to host endpoints for the backend"""
import io
import threading
import base64
from concurrent.futures import ThreadPoolExecutor
import os
from bson.binary import Binary
from flask import Flask, Response, request, jsonify, send_file
from kubernetes import client, config
import pymongo
from modules.mongo import upload_experiment_aggregated_results, upload_experiment_zip, upload_log_file, verify_mongo_connection, check_insert_default_experiments, download_experiment_file, get_experiment, update_exp_value, increment_exp_value, increment_finished_shards, upload_partial_results, upload_partial_artifacts, get_partial_results, get_partial_artifacts
from modules.mailSend import send_completion_email

from spawn_runner import create_job, create_job_object, create_finalize_job_object
from build_image import build_experiment_image
flaskApp = Flask(__name__)

config.load_incluster_config()
BATCH_API = client.BatchV1Api()
CORE_API = client.CoreV1Api()

# Experiment submission runs the (possibly long) build + Job spawn off the request
# thread. Threads (not processes) so the shared k8s clients above are reused and
# no experiment payload needs pickling.
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
executor = ThreadPoolExecutor(MAX_WORKERS)

# Mongo Setup
# create the mongo client
mongoClient = pymongo.MongoClient(
    "glados-service-mongodb",
    int(str(os.getenv("MONGODB_PORT"))),
    username=str(os.getenv("MONGODB_USERNAME")),
    password=str(os.getenv("MONGODB_PASSWORD")),
    authMechanism='SCRAM-SHA-256',
    serverSelectionTimeoutMS=1000,
    replicaSet='rs0'
)
# connect to the glados database
gladosDB = mongoClient["gladosdb"]
# call the function to check if the documents for default experiments exist
# start that in a different thread so that it can do its thing in peace
addDefaultExpsThread = threading.Thread(target=check_insert_default_experiments, args={mongoClient})
addDefaultExpsThread.start()

# setup the mongo collections
experimentsCollection = gladosDB.experiments
resultsCollection = gladosDB.results
resultZipCollection = gladosDB.zips
logCollection = gladosDB.logs

@flaskApp.route('/')
def hello_world():
    """For testing if there is a connection to the backend"""
    return Response(status=200)

@flaskApp.get("/queue")
def get_queue():
    """The query to get the size of the queue"""
    # ThreadPoolExecutor exposes no public API for the pending-work count, so we
    # read the internal dict directly. pylint can't see this dynamically-created
    # member and flags protected-access; both are expected here.
    # pylint: disable=protected-access,no-member
    return jsonify({"queueSize": len(executor._pending_work_items)})

@flaskApp.post("/experiment")
def recv_experiment():
    """The query to run an experiment"""
    data = request.get_json()
    future = executor.submit(spawn_job, data)
    future.add_done_callback(_log_spawn_result)
    return Response(status=200)

def _log_spawn_result(future):
    """Surface exceptions raised by the background spawn_job worker.

    ThreadPoolExecutor stores a worker's exception on the Future and never
    re-raises it, so without this callback a failed experiment launch (e.g. an
    image build error or a Kubernetes API 403) would vanish silently -- the
    /experiment request has already returned 200 by the time it runs.
    """
    error = future.exception()
    if error is not None:
        flaskApp.logger.error("Experiment launch failed in background worker", exc_info=error)

def spawn_job(experiment_data):
    """Build the per-experiment image (if dependencies are declared) then spawn the runner Job.

    The /experiment payload only carries the experiment id, so the declared
    dependencies are read from MongoDB and attached before building. If that
    lookup fails or no dependencies are declared, build_experiment_image returns
    None and create_job_object falls back to the default runner image (unchanged
    behaviour for experiments without declared dependencies).
    """
    try:
        stored = get_experiment(experiment_data['experiment']['id'], mongoClient)
        experiment_data['experiment']['pipRequirements'] = stored.get('pipRequirements')
        experiment_data['experiment']['aptPackages'] = stored.get('aptPackages')
        # Number of runner-pod shards to spread this experiment's trials across.
        # Drives the Indexed Job's parallelism/completions in create_job_object.
        experiment_data['experiment']['workers'] = stored.get('workers')
    except Exception:
        # Non-fatal: fall back to the default runner image (no per-experiment build),
        # but log it so a Mongo/lookup problem is visible rather than silent.
        flaskApp.logger.warning(
            "Could not read declared dependencies for experiment %s; proceeding without a per-experiment image",
            experiment_data.get('experiment', {}).get('id'),
            exc_info=True,
        )

    image_override = build_experiment_image(experiment_data, BATCH_API, CORE_API)
    job = create_job_object(experiment_data, image_override=image_override)
    create_job(BATCH_API, job)
    
@flaskApp.post("/cancelExperiment")
def cancel_experiment():
    """The query to cancel an experiment"""
    data = request.get_json()
    job_name = data['jobName']
    # kill the runner Job (a single Indexed Job owns every shard pod, so deleting
    # it by name removes all shards at once)
    BATCH_API.delete_namespaced_job(job_name, "default", propagation_policy="Background")
    # Also remove the finalize Job if it was already spawned. It is a separate
    # Job (runner-<expId>-finalize), so cancelling the runner does not touch it.
    # Best-effort: a not-yet-spawned finalize job 404s, which is fine.
    try:
        BATCH_API.delete_namespaced_job(job_name + "-finalize", "default", propagation_policy="Background")
    except Exception:
        pass
    return Response(status=200)

def spawn_finalize_job(experiment_id):
    """Spawn the one-shot finalize Job once every shard has reported complete.

    The runner shards each upload a partial results CSV + partial artifacts zip
    and only $inc the progress counters; none of them owns the terminal
    aggregation. This finalize Job (runner image, --finalize mode) merges the
    partials into the single results.csv/zip/plot, sends the one completion
    email, and writes the terminal finished/status fields.
    """
    job = create_finalize_job_object(experiment_id)
    create_job(BATCH_API, job)

@flaskApp.post("/shardComplete")
def shard_complete():
    """A runner-pod shard reports it has finished; trigger finalize on the last one.

    Atomically increments finishedShards and, when it reaches the experiment's
    worker count, spawns the finalize Job exactly once (find_one_and_update hands
    out distinct counts, so only one shard sees finishedShards == workers).
    """
    try:
        experiment_id = request.get_json()['experimentId']
        finished_shards, workers = increment_finished_shards(experiment_id, mongoClient)
        if finished_shards >= workers:
            executor.submit(spawn_finalize_job, experiment_id).add_done_callback(_log_spawn_result)
        return Response(status=200)
    except Exception:
        return Response(status=500)
    
@flaskApp.post("/sendEmail")
def send_email():
    """Send an experiment-completion email on the runner's behalf.

    The runner used to send this itself, which required shipping the Gmail
    credentials and internet egress into the untrusted runner pod. It now POSTs
    the display fields here and the backend performs the send. Best-effort:
    always returns 200 so a mail failure never fails the experiment run.
    """
    data = request.get_json()
    send_completion_email(
        data.get('email'),
        data.get('name'),
        data.get('status'),
        data.get('passes'),
        data.get('fails'),
    )
    return Response(status=200)

@flaskApp.post("/uploadResults")
def upload_results():
    json = request.get_json()
    # Get JSON requests
    experimentId = json['experimentId']
    results = json['results']
    # now call the mongo stuff
    return {'id': upload_experiment_aggregated_results(experimentId, results, mongoClient)}

@flaskApp.post("/uploadZip")
def upload_zip():
    json = request.get_json()
    # Get JSON requests
    experimentId = json['experimentId']
    encoded = Binary(base64.b64decode(json['encoded']))
    return {'id': upload_experiment_zip(experimentId, encoded, mongoClient)}

@flaskApp.post("/uploadPartialResults")
def upload_partial_results_route():
    """Store one shard's partial results CSV (its header + its rows)."""
    json = request.get_json()
    experimentId = json['experimentId']
    shardIndex = json['shardIndex']
    results = json['results']
    return {'id': upload_partial_results(experimentId, shardIndex, results, mongoClient)}

@flaskApp.post("/uploadPartialArtifacts")
def upload_partial_artifacts_route():
    """Store one shard's partial ResCsvs zip (its per-trial logs/extra files)."""
    json = request.get_json()
    experimentId = json['experimentId']
    shardIndex = json['shardIndex']
    encoded = Binary(base64.b64decode(json['encoded']))
    return {'id': upload_partial_artifacts(experimentId, shardIndex, encoded, mongoClient)}

@flaskApp.post("/getPartialResults")
def get_partial_results_route():
    """Return every shard's partial results CSV for the finalize job to merge."""
    try:
        experimentId = request.get_json()['experimentId']
        return {'partials': get_partial_results(experimentId, mongoClient)}
    except Exception:
        return Response(status=500)

@flaskApp.post("/getPartialArtifacts")
def get_partial_artifacts_route():
    """Return every shard's partial ResCsvs zip (base64) for the finalize job."""
    try:
        experimentId = request.get_json()['experimentId']
        return {'partials': get_partial_artifacts(experimentId, mongoClient)}
    except Exception:
        return Response(status=500)

@flaskApp.post("/uploadLog")
def upload_log():
    json = request.get_json()
    # Get JSON requests
    experimentId = json['experimentId']
    logContents = json['logContents']
    return {'id': upload_log_file(experimentId, logContents, mongoClient)}
    
@flaskApp.get("/mongoPulse")
def check_mongo():
    try:
        verify_mongo_connection(mongoClient)
        return Response(status=200)
    except Exception:
        return Response(status=500)
    
@flaskApp.get("/downloadExpFile")
def download_exp_file():
    try:
        file_id = request.args.get('fileId', default='', type=str)
        file_data = download_experiment_file(file_id, mongoClient)
        file_stream = io.BytesIO(file_data)
        return send_file(file_stream, as_attachment=True, download_name="experiment_file", mimetype="application/octet-stream")
    except Exception:
        return Response(status=500)
    
@flaskApp.post("/getExperiment")
def get_experiment_post():
    try:
        experiment_id = request.get_json()['experimentId']
        return {'contents': get_experiment(experiment_id, mongoClient)}
    except Exception:
        return Response(status=500)
    
@flaskApp.post("/updateExperiment")
def update_experiment():
    try:
        json = request.get_json()
        experiment_id = json['experimentId']
        field = json['field']
        newVal = json['newValue']
        update_exp_value(experiment_id, field, newVal, mongoClient)
        return Response(status=200)
    except Exception:
        return Response(status=500)

@flaskApp.post("/incrementExperimentValue")
def increment_experiment():
    """Atomically $inc a numeric experiment field (e.g. passes/fails).

    Runner shards run concurrently, so progress counters must be summed
    atomically rather than $set to a per-pod absolute (which would clobber the
    other shards' progress).
    """
    try:
        json = request.get_json()
        experiment_id = json['experimentId']
        field = json['field']
        amount = json.get('amount', 1)
        increment_exp_value(experiment_id, field, amount, mongoClient)
        return Response(status=200)
    except Exception:
        return Response(status=500)

if __name__ == '__main__':
    flaskApp.run()
