"""Module that provides functionality of the runner"""
import os
import csv
import base64
import shutil
import logging
import sys
import json
import time
import typing

import requests
from bson.binary import Binary

from modules.data.types import DocumentId, IncomingStartRequest
from modules.data.experiment import ExperimentData, ExperimentType
from modules.data.parameters import Parameter, parseRawHyperparameterData
#from modules.db.mongo import upload_experiment_aggregated_results, upload_experiment_log, upload_experiment_zip, verify_mongo_connection
from modules.logging.gladosLogging import EXPERIMENT_LOGGER, SYSTEM_LOGGER, close_experiment_logger, configure_root_logger, open_experiment_logger
from modules.runner import conduct_experiment, merge_partial_result_contents
from modules.exceptions import CustomFlaskError, DatabaseConnectionError, GladosInternalError, GladosUserError
from modules.output.plots import generateScatterPlot
from modules.configs import generate_config_files
from modules.utils import _get_env, upload_experiment_aggregated_results, upload_experiment_log, upload_experiment_zip, verify_mongo_connection, get_experiment_with_id, update_exp_value, request_completion_email, report_shard_complete, upload_partial_results, upload_partial_artifacts, get_partial_results, get_partial_artifacts

try:
    import magic  # Crashes on windows if you're missing the 'python-magic-bin' python package
except ImportError:
    logging.error("Failed to import the 'magic' package, you're probably missing a system level dependency")
    sys.exit(1)

DB_COLLECTION_EXPERIMENTS = "Experiments"

# set up logger
configure_root_logger()
syslogger = logging.getLogger(SYSTEM_LOGGER)
explogger = logging.getLogger(EXPERIMENT_LOGGER)


syslogger.info("GLADOS Runner Started")

def main(experiment_data: str):
    """Function that gets called when the file is ran"""

    if _check_request_integrity(experiment_data):
        run_batch_and_catch_exceptions(json.loads(experiment_data))
        return
    
    syslogger.error("Received malformed experiment request: %s", experiment_data)

def _check_request_integrity(data: typing.Any):
    print(data)
    try:
        json_data = json.loads(data)
        return json_data['experiment']['id'] is not None
    except KeyError:
        return False
    
def run_batch_and_catch_exceptions(data: IncomingStartRequest):
    try:
        run_batch(data)
    except Exception as err:
        syslogger.error("Unexpected exception while trying to run the experiment, this was not caught by our own code and needs to be handled better.")
        syslogger.exception(err)
        close_experiment_logger()
        # Unsafe to upload experiment logs files here
        raise err


def run_batch(data: IncomingStartRequest):
    syslogger.info('Run_Batch starting with data %s', data)

    # Obtain most basic experiment info
    exp_id = data['experiment']['id']
    syslogger.debug('received %s', exp_id)

    # This pod is one shard of an Indexed Job; k8s injects its 0-based index.
    # numShards is read from the experiment doc's `workers` field once loaded.
    shardIndex = int(os.getenv("JOB_COMPLETION_INDEX", "0"))
    syslogger.info('Running as shard %s', shardIndex)

    # Only shard 0 flips the experiment to RUNNING to avoid redundant/racing writes.
    if shardIndex == 0:
        update_exp_value(exp_id, "status", "RUNNING")

    open_experiment_logger(exp_id)

    # Retrieve experiment details from the backend api
    try:
        experiment_data = get_experiment_with_id(exp_id)


    except Exception as err:  # pylint: disable=broad-exception-caught
        explogger.error("Error retrieving experiment data from mongo, aborting")
        explogger.exception(err)
        close_shard_run(exp_id, shardIndex)
        return

    # Parse hyperaparameters into their datatype. Required to parse the rest of the experiment data
    try:
        hyperparameters: "dict[str,Parameter]" = parseRawHyperparameterData(experiment_data['hyperparameters'])
    except (KeyError, ValueError) as err:
        if isinstance(err, KeyError):
            explogger.error("Error generating hyperparameters - hyperparameters not found in experiment object, aborting")
        else:
            explogger.error("Error generating hyperparameters - Validation error")
        explogger.exception(err)
        close_shard_run(exp_id, shardIndex)
        return
    experiment_data['hyperparameters'] = hyperparameters

    # Parsing into Datatype
    try:
        experiment = ExperimentData(**experiment_data)
        experiment.postProcess = experiment.scatter
    except ValueError as err:
        explogger.error("Experiment data was not formatted correctly, aborting")
        explogger.exception(err)
        close_shard_run(exp_id, shardIndex)
        return

    #Downloading Experiment File
    # If the program errors here after you just deleted the ExperimentFiles on your dev machine, restart the docker container, seems to be volume mount weirdness
    os.makedirs(f'ExperimentFiles/{exp_id}')
    os.chdir(f'ExperimentFiles/{exp_id}')
    filepath = download_experiment_files(experiment)

    try:
        experiment.experimentType = determine_experiment_file_type(filepath)
    except NotImplementedError as err:
        explogger.error("This is not a supported experiment file type, aborting")
        explogger.exception(err)
        os.chdir('../..')
        close_shard_run(exp_id, shardIndex)
        return
    
    # If it is a zip file, extract it
    if experiment.experimentType == ExperimentType.ZIP:
        try:
            # rename the file to a zip file
            os.rename(filepath, "userProvidedFile.zip")
            shutil.unpack_archive("userProvidedFile.zip", '.')
        except Exception as err:
            explogger.error("Failed to extract zip file")
            explogger.exception(err)
            os.chdir('../..')
            close_shard_run(exp_id, shardIndex)
            return
    
    # Dependencies are baked into the per-experiment image at build time (see
    # backend build_image.py); the runner installs nothing at runtime. This is
    # required for the hardened runner (non-root + readOnlyRootFilesystem) and
    # removes the former admin apt-get / arbitrary-command install paths.
    if experiment.experimentType == ExperimentType.ZIP:
        # Recalc the file type
        try:
            experiment.experimentType = determine_experiment_file_type(experiment.experimentExecutable)
            # also update experiment.file
            experiment.file = experiment.experimentExecutable
            explogger.info(f"New experiment file type: {experiment.experimentType}")
        except NotImplementedError as err:
            explogger.error("This is not a supported experiment file type, aborting")
            explogger.exception(err)
            os.chdir('../..')
            close_shard_run(exp_id, shardIndex)
            return
      

    explogger.info(f"Generating configs and downloading to ExperimentFiles/{exp_id}/configFiles")

    totalExperimentRuns = generate_config_files(experiment)
    if totalExperimentRuns == 0:
        os.chdir('../..')
        explogger.exception(GladosInternalError("Error generating configs - somehow no config files were produced?"))
        close_shard_run(exp_id, shardIndex)
        return

    experiment.totalExperimentRuns = totalExperimentRuns

    # generate_config_files is deterministic, so every shard computes the same
    # total; only shard 0 needs to persist it.
    numShards = experiment.workers or 1
    if shardIndex == 0:
        update_exp_value(exp_id, "totalExperimentRuns", experiment.totalExperimentRuns)

    try:
        # Run only this shard's slice of trials and upload the partial artifacts.
        # Aggregation, plotting, the zip, the email, and the terminal status are
        # all owned by the finalize job (spawned by the backend once every shard
        # reports complete) -- a shard must not do them or the outputs would be
        # per-pod partials overwriting each other.
        conduct_experiment(experiment, shardIndex, numShards)
        upload_shard_results(experiment, shardIndex)
        upload_shard_artifacts(experiment, shardIndex)
    except Exception as err:  # pylint: disable=broad-exception-caught
        explogger.error('Uncaught exception while running an experiment shard. The GLADOS code needs to be changed to handle this in a cleaner manner')
        explogger.exception(err)
    finally:
        # We need to be out of the dir for the log file upload to work
        os.chdir('../..')
        close_shard_run(exp_id, shardIndex)

def close_shard_run(expId: DocumentId, shardIndex: int):
    """Wind down one runner-pod shard.

    Uploads this shard's log and cleans up, then -- always, even after a failure
    -- reports the shard complete so the backend's finishedShards counter
    advances and the finalize job eventually runs. Unlike the old
    close_experiment_run, this does NOT write finished/status/finishedAt: those
    terminal fields are owned by the finalize job so they are set exactly once,
    after every shard's results are merged.
    """
    explogger.info(f'Exiting experiment {expId} shard {shardIndex}')
    try:
        close_experiment_logger()
        upload_experiment_log(expId)
    except Exception as err:  # pylint: disable=broad-exception-caught
        syslogger.error("Failed to upload log for shard %s: %s", shardIndex, err)
    try:
        remove_downloaded_directory(expId)
    except Exception as err:  # pylint: disable=broad-exception-caught
        syslogger.error("Failed to clean up dir for shard %s: %s", shardIndex, err)
    # Report last, and defensively, so a failed shard still advances finishedShards.
    try:
        report_shard_complete(expId)
    except Exception as err:  # pylint: disable=broad-exception-caught
        syslogger.error("Failed to report shard %s complete: %s", shardIndex, err)

def upload_shard_results(experiment: ExperimentData, shardIndex: int):
    """Upload this shard's partial results.csv for the finalize job to merge."""
    verify_mongo_connection()
    try:
        with open('results.csv', 'r', encoding="utf8") as experimentFile:
            resultContent = experimentFile.read()
    except Exception as err:
        raise GladosInternalError("Failed to read partial result file for upload to mongodb") from err
    upload_partial_results(experiment.expId, shardIndex, resultContent)

def upload_shard_artifacts(experiment: ExperimentData, shardIndex: int):
    """Bundle this shard's ResCsvs (per-trial logs/extra files) + its config files.

    The finalize job unpacks every shard's bundle into one ResCsvs to build the
    complete results zip.
    """
    try:
        if os.path.exists('configFiles'):
            shutil.copytree('configFiles', 'ResCsvs/configFiles', dirs_exist_ok=True)
    except Exception as err:
        raise GladosInternalError("Error copying config files to ResCsvs") from err
    try:
        shutil.make_archive(f'PartialCsvs{shardIndex}', 'zip', 'ResCsvs')
        with open(f"PartialCsvs{shardIndex}.zip", "rb") as file:
            encoded = Binary(file.read())
    except Exception as err:
        raise GladosInternalError("Error preparing partial results zip") from err
    upload_partial_artifacts(experiment.expId, shardIndex, encoded)

def determine_experiment_file_type(filepath: str):
    try:
        rawfiletype = magic.from_file(filepath)
        filetype = ExperimentType.UNKNOWN
        if 'Python script' in rawfiletype or 'python3' in rawfiletype:
            filetype = ExperimentType.PYTHON
        elif 'Java archive data (JAR)' in rawfiletype:
            filetype = ExperimentType.JAVA
        elif 'ELF 64-bit LSB' in rawfiletype:
            filetype = ExperimentType.C
        # check if file is zip
        elif 'Zip archive data' in rawfiletype:
            filetype = ExperimentType.ZIP

        explogger.info(f"Raw Filetype: {rawfiletype}, Filtered Filetype: {filetype.value}")

        if filetype == ExperimentType.UNKNOWN:
            explogger.error(f'File type "{rawfiletype}" could not be mapped to Python, Java or C, if it should consider updating the matching statements')
            raise NotImplementedError("Unknown experiment file type")
        return filetype
    except Exception as err:
        explogger.error('Error determining file type')
        explogger.exception(err)
        raise NotImplementedError("Unknown experiment file type") from err

def download_experiment_files(experiment: ExperimentData):
    explogger.info('There will be experiment outputs')
    os.makedirs('ResCsvs')
    explogger.info(f'Downloading file for {experiment.expId}')

    filepath = experiment.file
    explogger.info(f"Downloading {filepath} to ExperimentFiles/{experiment.expId}/{filepath}")
    try:
        # try to call the backend to download
        url = f'http://glados-service-backend:{os.getenv("BACKEND_PORT")}/downloadExpFile?fileId={experiment.file}'
        response = requests.get(url, timeout=60)
        file_contents = response.content
        # write the file contents to file path
        with open(filepath, "xb") as file:
            file.write(file_contents)
        
    except Exception as err:
        explogger.error(f"Error {err} occurred while trying to download experiment file")
        raise GladosInternalError('Failed to download experiment files') from err
    explogger.info(f"Downloaded {filepath} to ExperimentFiles/{experiment.expId}/{filepath}")
    return filepath

def remove_downloaded_directory(experimentId: DocumentId):
    
    folder_name = experimentId
    target_directory = "ExperimentFiles"
    folder_path = f'{target_directory}/{ folder_name}'
    explogger.info("this is the path " + folder_path)
    explogger.info("Does the path exist? " + str(os.path.exists(folder_path)))
    items = os.listdir(target_directory)
    
    try:
        shutil.rmtree(folder_path)
        explogger.info("The folder directory " + folder_path + " successfully deleted.")
    except FileNotFoundError as err:
        explogger.error('Error during plot generation')
        explogger.exception(err)

def post_process_experiment(experiment: ExperimentData):
    if experiment.postProcess:
        explogger.info("Beginning post processing")
        try:
            if experiment.scatter:
                explogger.info("Creating Scatter Plot")
                depVar = experiment.scatterDepVar
                indVar = experiment.scatterIndVar
                generateScatterPlot(indVar, depVar, 'results.csv', experiment.expId)
        except (KeyError, ValueError) as err:
            explogger.error('Error during plot generation')
            explogger.exception(err)
            
def send_email(experiment: ExperimentData, status: str):
    if experiment.sendEmail:
        explogger.info(f"Requesting completion email to {experiment.creatorEmail}")
        experiment.status = status
        # The backend holds the Gmail credentials and does the actual send; the
        # runner just forwards the display fields (see request_completion_email).
        request_completion_email(
            experiment.creatorEmail,
            experiment.name,
            experiment.status,
            experiment.passes,
            experiment.fails,
        )


# --- Finalize mode ---------------------------------------------------------
# Spawned by the backend as a one-shot Job (runner-<expId>-finalize) once every
# shard has reported complete. It merges each shard's partial results/artifacts
# into the single aggregated results.csv/zip/plot, sends the one completion
# email, and writes the terminal finished/status fields.

def finalize_main(experiment_data: str):
    """Entry point for the finalize Job (`runner.py --finalize <json>`)."""
    if _check_request_integrity(experiment_data):
        finalize_and_catch_exceptions(json.loads(experiment_data))
        return
    syslogger.error("Received malformed finalize request: %s", experiment_data)

def finalize_and_catch_exceptions(data: IncomingStartRequest):
    try:
        finalize_experiment(data)
    except Exception as err:
        syslogger.error("Unexpected exception during finalize; the experiment may not be marked finished.")
        syslogger.exception(err)
        close_experiment_logger()
        raise err

def finalize_experiment(data: IncomingStartRequest):
    exp_id = data['experiment']['id']
    syslogger.info('Finalizing experiment %s', exp_id)
    open_experiment_logger(exp_id)

    # Load the (now complete) experiment doc so passes/fails/email fields are final.
    try:
        experiment_data = get_experiment_with_id(exp_id)
        experiment_data['hyperparameters'] = parseRawHyperparameterData(experiment_data['hyperparameters'])
        experiment = ExperimentData(**experiment_data)
        experiment.postProcess = experiment.scatter
    except Exception as err:  # pylint: disable=broad-exception-caught
        explogger.error("Finalize: failed to load experiment data, marking failed")
        explogger.exception(err)
        _finish_experiment(exp_id, "FAILED", None)
        return

    os.makedirs(f'ExperimentFiles/{exp_id}', exist_ok=True)
    os.chdir(f'ExperimentFiles/{exp_id}')
    try:
        mergedRows, header = _merge_partial_results(exp_id)
        if header is None:
            explogger.error("Finalize: no shard produced results; marking experiment failed")
            os.chdir('../..')
            _finish_experiment(exp_id, "FAILED", experiment)
            return

        with open('results.csv', 'w', encoding="utf8") as expResults:
            writer = csv.writer(expResults)
            writer.writerow(header)
            writer.writerows(mergedRows)

        # Rebuild the full ResCsvs from every shard's partial bundle, then plot,
        # aggregate, and zip -- the same artifacts a single-pod run produced.
        os.makedirs('ResCsvs', exist_ok=True)
        _merge_partial_artifacts(exp_id)

        post_process_experiment(experiment)

        verify_mongo_connection()
        with open('results.csv', 'r', encoding="utf8") as experimentFile:
            upload_experiment_aggregated_results(experiment, experimentFile.read())

        try:
            shutil.copy2('results.csv', 'ResCsvs/results.csv')
            shutil.make_archive('ResultCsvs', 'zip', 'ResCsvs')
            with open("ResultCsvs.zip", "rb") as file:
                encoded = Binary(file.read())
        except Exception as err:
            raise GladosInternalError("Error preparing merged experiment results zip") from err
        upload_experiment_zip(experiment, encoded)

        os.chdir('../..')
        _finish_experiment(exp_id, "COMPLETED", experiment)
    except Exception as err:  # pylint: disable=broad-exception-caught
        explogger.error("Finalize: error while aggregating shard results")
        explogger.exception(err)
        try:
            os.chdir('../..')
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        _finish_experiment(exp_id, "FAILED", experiment)

def _merge_partial_results(exp_id: DocumentId):
    """Pull every shard's partial results CSV from the backend and merge them.

    Delegates the CSV merge to modules.runner.merge_partial_result_contents.
    """
    partials = get_partial_results(exp_id)
    return merge_partial_result_contents(partials)

def _merge_partial_artifacts(exp_id: DocumentId):
    """Unpack every shard's partial ResCsvs zip into the combined ResCsvs dir."""
    partials = get_partial_artifacts(exp_id)
    for partial in partials:
        try:
            raw = base64.b64decode(partial['encoded'])
            tmpZip = f"partial_{partial['shardIndex']}.zip"
            with open(tmpZip, 'wb') as zipFile:
                zipFile.write(raw)
            shutil.unpack_archive(tmpZip, 'ResCsvs')
            os.remove(tmpZip)
        except Exception as err:  # pylint: disable=broad-exception-caught
            explogger.error("Finalize: failed to unpack partial artifacts for shard %s", partial.get('shardIndex'))
            explogger.exception(err)

def _finish_experiment(exp_id: DocumentId, status: str, experiment: typing.Optional[ExperimentData]):
    """Write the terminal experiment fields exactly once and send the one email."""
    update_exp_value(exp_id, 'status', status)
    update_exp_value(exp_id, 'finished', True)
    update_exp_value(exp_id, 'finishedAtEpochMilliseconds', int(time.time() * 1000))
    if experiment is not None:
        send_email(experiment, status)
    try:
        close_experiment_logger()
        upload_experiment_log(exp_id)
    except Exception as err:  # pylint: disable=broad-exception-caught
        syslogger.error("Finalize: failed to upload finalize log: %s", err)
    remove_downloaded_directory(exp_id)


if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == '--finalize':
        finalize_main(args[1])
    elif len(args) == 1:
        main(args[0])
    else:
        raise ValueError("Error: expected `<json>` (runner) or `--finalize <json>` (finalize job)")
