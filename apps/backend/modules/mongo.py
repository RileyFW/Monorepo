import json
import base64
import pymongo
from pymongo.errors import ConnectionFailure
from bson.objectid import ObjectId
from bson.binary import Binary
from gridfs import GridFSBucket

def verify_mongo_connection(mongoClient: pymongo.MongoClient):
    try:
        mongoClient.admin.command('ping')
    except ConnectionFailure as err:
        # just use a generic exception
        raise Exception("MongoDB server not available/unreachable") from err
    
def upload_experiment_aggregated_results(experimentId: str, results: str, mongoClient: pymongo.MongoClient):
    # Get the results connection
    resultsBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='resultsBucket')
    try:
        # Encode the results string to bytes
        results_bytes = results.encode('utf-8')
        # Now we need to store the results in the GridFS bucket
        resultId = resultsBucket.upload_from_stream(f"results{experimentId}", results_bytes, metadata={"experimentId": experimentId})
        # return the resultID
        return str(resultId)
        
    except Exception as err:
        # Change to generic exception
        raise Exception("Encountered error while storing aggregated results in MongoDB") from err
    
def upload_experiment_zip(experimentId: str, encoded: Binary, mongoClient: pymongo.MongoClient):
    zipsBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='zipsBucket')
    try:
        resultId = zipsBucket.upload_from_stream(f"results{experimentId}.zip", encoded, metadata={"experimentId": experimentId})
        return str(resultId)
    except Exception as err:
        raise Exception("Encountered error while storing results zip in MongoDB") from err
    
def upload_log_file(experimentId: str, contents: str, mongoClient: pymongo.MongoClient, shardLabel: str = None):
    logsBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='logsBucket')
    try:
        metadata = {"experimentId": experimentId}
        if shardLabel is not None:
            metadata["shardLabel"] = shardLabel
        resultId = logsBucket.upload_from_stream(f"log{experimentId}.txt", contents.encode('utf-8'), metadata=metadata)
        return str(resultId)
    except Exception as err:
        raise Exception("Encountered error while storing log file in MongoDB") from err
    
def check_insert_default_experiments(mongoClient: pymongo.MongoClient):
    # this gets run on its own thread, so let it try to enter the default experiments
    def insertExperiments():
        defaultExperimentCollection = mongoClient["gladosdb"].defaultExperiments
        experiments = [
            # python experiments
            {"name": "addNums.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/addNums.py"},
            {"name": "addNumsFailsOnXis1Yis5.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/addNumsFailsOnXis1Yis5.py"},
            {"name": "addNumsTimeOutOnXis1Yis5.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/addNumsTimeOutOnXis1Yis5.py"},
            {"name": "addNumsTimed.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/addNumsTimed.py"},
            {"name": "addNumsWithConstants.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/addNumsWithConstants.py"},
            {"name": "alwaysFail.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/alwaysFail.py"},
            {"name": "genetic_algorithm.py", "type": "python", "url": "https://raw.githubusercontent.com/AutomatingSciencePipeline/Monorepo/refs/heads/main/example_experiments/python/genetic_algorithm.py"}
            # C experiments
            # Java experiments
        ]
        
        for exp in experiments:
            count = defaultExperimentCollection.count_documents({"name": exp["name"]})
            if count == 0:
                defaultExperimentCollection.insert_one(exp)
                
    try:
        insertExperiments()
    except:
        # keep trying
        check_insert_default_experiments(mongoClient)
        
def download_experiment_file(file_id: str, mongoClient: pymongo.MongoClient):
    # we are going to have to get the binary data from mongo here
    # setup the bucket
    db = mongoClient["gladosdb"]
    bucket = GridFSBucket(db, bucket_name='fileBucket')
    files = bucket.find({"_id": ObjectId(file_id)}).to_list() # type: ignore
    num_files = 0
    file_name = ""
    for file in files:
        num_files += 1
        if num_files > 1:
            raise Exception("There are more than 1 file for a single experiment!")        
        file_name = file.filename
    if file_name == "":
        raise Exception("No file found!")
    file = bucket.open_download_stream_by_name(file_name)
    contents = file.read()
    return contents

def get_experiment(expId: str, mongoClient: pymongo.MongoClient):
    experimentsCollection = mongoClient["gladosdb"].experiments
    experiment = experimentsCollection.find_one({"_id": ObjectId(expId)}, {"_id": 0})
    if experiment is None:
        raise Exception("Could not find experiment!")
    experiment["id"] = expId
    experiment["expId"] = expId
    return experiment

def update_exp_value(expId: str, field: str, newValue: str, mongoClient: pymongo.MongoClient):
    experimentsCollection = mongoClient["gladosdb"].experiments
    experimentsCollection.update_one({"_id": ObjectId(expId)}, {"$set": {field: newValue}})
    return

def increment_exp_value(expId: str, field: str, amount, mongoClient: pymongo.MongoClient):
    """Atomically increment a numeric experiment field ($inc).

    Used for the progress counters (passes/fails) which are now written
    concurrently by multiple runner-pod shards. A $set of an absolute per-pod
    count would clobber the other shards' progress, so shards send a delta here
    and Mongo sums them atomically.
    """
    experimentsCollection = mongoClient["gladosdb"].experiments
    experimentsCollection.update_one({"_id": ObjectId(expId)}, {"$inc": {field: amount}})
    return

def increment_finished_shards(expId: str, mongoClient: pymongo.MongoClient):
    """Atomically record that one runner-pod shard has finished.

    Returns (finishedShards, workers) read from the post-increment document so the
    caller can detect the last shard (finishedShards == workers) exactly once and
    trigger the finalize job. find_one_and_update is atomic, so two shards
    finishing simultaneously each get a distinct finishedShards value.
    """
    experimentsCollection = mongoClient["gladosdb"].experiments
    updated = experimentsCollection.find_one_and_update(
        {"_id": ObjectId(expId)},
        {"$inc": {"finishedShards": 1}},
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if updated is None:
        raise Exception("Could not find experiment to record shard completion!")
    return updated.get("finishedShards", 0), updated.get("workers", 1)

def upload_partial_results(experimentId: str, shardIndex: int, results: str, mongoClient: pymongo.MongoClient):
    """Store one shard's partial results CSV (its rows + a header) in GridFS.

    Keyed by experimentId + shardIndex so the finalize job can pull every shard's
    partial and merge them into the single aggregated results.csv.
    """
    partialBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='partialResultsBucket')
    try:
        resultId = partialBucket.upload_from_stream(
            f"partial{experimentId}-{shardIndex}",
            results.encode('utf-8'),
            metadata={"experimentId": experimentId, "shardIndex": shardIndex},
        )
        return str(resultId)
    except Exception as err:
        raise Exception("Encountered error while storing partial results in MongoDB") from err

def upload_partial_artifacts(experimentId: str, shardIndex: int, encoded: Binary, mongoClient: pymongo.MongoClient):
    """Store one shard's partial ResCsvs zip (its per-trial logs/extra files)."""
    partialBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='partialArtifactsBucket')
    try:
        resultId = partialBucket.upload_from_stream(
            f"partial{experimentId}-{shardIndex}.zip",
            encoded,
            metadata={"experimentId": experimentId, "shardIndex": shardIndex},
        )
        return str(resultId)
    except Exception as err:
        raise Exception("Encountered error while storing partial artifacts in MongoDB") from err

def get_partial_results(experimentId: str, mongoClient: pymongo.MongoClient):
    """Return every shard's partial results CSV for an experiment.

    Returns a list of {"shardIndex": int, "results": str} for the finalize job to
    merge. Ordering is not guaranteed; the finalize step sorts merged rows itself.
    """
    partialBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='partialResultsBucket')
    partials = []
    for grid_file in partialBucket.find({"metadata.experimentId": experimentId}):
        shardIndex = (grid_file.metadata or {}).get("shardIndex", 0)
        partials.append({"shardIndex": shardIndex, "results": grid_file.read().decode('utf-8')})
    return partials

def get_partial_artifacts(experimentId: str, mongoClient: pymongo.MongoClient):
    """Return every shard's partial ResCsvs zip, base64-encoded, for the finalize job."""
    partialBucket = GridFSBucket(mongoClient["gladosdb"], bucket_name='partialArtifactsBucket')
    partials = []
    for grid_file in partialBucket.find({"metadata.experimentId": experimentId}):
        shardIndex = (grid_file.metadata or {}).get("shardIndex", 0)
        encoded = base64.b64encode(grid_file.read()).decode('utf-8')
        partials.append({"shardIndex": shardIndex, "encoded": encoded})
    return partials
