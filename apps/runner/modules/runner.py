import csv
import io
import shutil
from subprocess import Popen, PIPE, TimeoutExpired
import time
import os

# from modules.data.trial import Trial
from modules.configs import create_config_from_data, create_yaml_from_data, get_configs_ordered_ini, get_configs_ordered_yaml
from modules.data.experiment import ExperimentData, ExperimentType
from modules.exceptions import FileHandlingError, GladosInternalError, GladosUserError, TrialTimeoutError
from modules.exceptions import InternalTrialFailedError
from modules.configs import get_config_paramNames_ini, get_config_paramNames_yaml
from modules.logging.gladosLogging import get_experiment_logger
from modules.utils import update_exp_value, increment_exp_value

from concurrent.futures import ProcessPoolExecutor, as_completed

PROCESS_OUT_STREAM = 0
PROCESS_ERROR_STREAM = 1

# Environment variables that are safe to expose to user-submitted trial code.
# User experiments are untrusted and run as a subprocess, so they must NOT
# inherit the runner's full environment: doing so leaks the secrets injected
# into the runner pod (GMAIL_CREDS, MONGODB_PORT, BACKEND_PORT, etc.) to
# arbitrary uploaded code. We pass through only the variables actually needed to
# locate and launch interpreters/binaries; everything else is withheld.
_TRIAL_ENV_ALLOWLIST = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR', 'JAVA_OPTS')

explogger = get_experiment_logger()


def _build_trial_env():
    """Build a sanitized environment for user-submitted trial subprocesses.

    Returns a copy of the runner's environment restricted to
    ``_TRIAL_ENV_ALLOWLIST`` so untrusted trial code cannot read the runner's
    injected secrets.
    """
    return {key: os.environ[key] for key in _TRIAL_ENV_ALLOWLIST if key in os.environ}


def _get_data(process: 'Popen[str]', trialRun: int, trialTimeout: int):
    try:
        data = process.communicate(timeout=trialTimeout)
        os.chdir('../ResCsvs')
        with open(f"log{trialRun}.txt", 'w', encoding='utf8') as trialLogFile:
            trialLogFile.write(data[PROCESS_OUT_STREAM])
            if data[1]:
                trialLogFile.write(data[PROCESS_ERROR_STREAM])
            trialLogFile.close()
        os.chdir('..')
        if data[PROCESS_ERROR_STREAM]:
            # pybullet build time is a common error that is not an error
            # the pybullet developers made this output on stderr because they are horrible developers
            # just ignore it for now, try to find a fix for this later
            if "pybullet build time" in data[PROCESS_ERROR_STREAM]:
                return
            errorMessage = f'errors returned from pipe is {data[PROCESS_ERROR_STREAM]}'
            explogger.error(errorMessage)
            raise InternalTrialFailedError(errorMessage)
    except TimeoutExpired as timeErr:
        explogger.error(f"{timeErr} Trial timed out")
        raise TrialTimeoutError("Trial took too long to complete") from timeErr
    except Exception as err:
        explogger.error("Encountered another exception while reading pipe: {err}")
        raise InternalTrialFailedError("Encountered another exception while reading pipe") from err


def _run_trial(experiment: ExperimentData, config_path: str, trialRun: int):
    """
    make sure that the cwd is ExperimentsFiles/{ExperimentId}/trial{trialNum}
    """
    # set the paths
    os.mkdir(f'trial{trialRun}')
    os.chdir(f'trial{trialRun}')
    # Untrusted user code: run with a sanitized environment so it cannot read
    # the runner's secrets (see _build_trial_env).
    trial_env = _build_trial_env()
    if experiment.experimentType == ExperimentType.PYTHON:
        with Popen(['python', "../" + experiment.file, config_path], stdout=PIPE, stdin=PIPE, stderr=PIPE, encoding='utf8', env=trial_env) as process:
            _get_data(process, trialRun, experiment.timeout)
    elif experiment.experimentType == ExperimentType.JAVA:
        with Popen(['java', '-jar', "../" + experiment.file, config_path], stdout=PIPE, stdin=PIPE, stderr=PIPE, encoding='utf8', env=trial_env) as process:
            _get_data(process, trialRun, experiment.timeout)
    elif experiment.experimentType == ExperimentType.C:
        Popen(['chmod', '+x', "../" + experiment.file], stdout=PIPE, stdin=PIPE, stderr=PIPE, encoding='utf8', env=trial_env)
        with Popen(['../' + experiment.file, config_path], stdout=PIPE, stdin=PIPE, stderr=PIPE, encoding='utf8', env=trial_env) as process:
            _get_data(process, trialRun, experiment.timeout)


def _get_line_n_of_trial_results_csv(targetLineNumber: int, filename: str):
    try:
        with open(filename, mode='r', encoding="utf8") as file:
            reader = csv.reader(file)
            lineNum = 0
            currLine = None
            for line in reader:
                currLine = line
                if lineNum == targetLineNumber:
                    return line
                lineNum += 1
            
            if targetLineNumber == -1:
                return currLine        
                    
            if lineNum == 0:
                raise GladosUserError(f"{filename} is an empty file cannot gather any information")
            if lineNum == 1:
                raise GladosUserError(f"{filename} only has one line. Potentially only has a Header or Value row?")
            raise GladosInternalError(f"Failed to get line {targetLineNumber} of {filename}")
    except Exception as err:
        raise GladosUserError("Failed to read trial results csv, does the file exist? Typo in the user-specified output filename(s)?") from err


def _add_to_output_batch(trialExtraFile: str, ExpRun: int):
    try:
        # check if this is directory
        if os.path.isdir(trialExtraFile):
            extraFileName = trialExtraFile.split('/')[-1]
            if extraFileName == "":
                extraFileName = trialExtraFile.split('/')[-2]
            # recursively copy the directory
            shutil.copytree(trialExtraFile, f'ResCsvs/{extraFileName}{ExpRun}')
        else:
            extraFileName = trialExtraFile.split('/')[-1].split('.')[0]
            shutil.copy2(f'{trialExtraFile}', f'ResCsvs/{extraFileName}{ExpRun}.csv')
    except Exception as err:
        explogger.error(f"Expected to find trial extra file at {trialExtraFile}")
        raise FileHandlingError("Failed to copy results csv. Maybe there was a typo in the filepath?") from err
   
    
def _run_trial_wrapper(experiment: ExperimentData, trialNum: int):
    """Run a single trial and return its results row, or None if the trial failed.

    Runs in a ProcessPoolExecutor worker. The header/param-name schema is derived
    from *this trial's own* config file rather than config 0: under sharding a pod
    may not own trial 0, so `configFiles/0.ini` need not exist here. Every config
    shares the same parameter keys, so any trial's config yields the same schema.
    """
    explogger.info(f"Running Trial {trialNum}")

    try:
        if(experiment.configFileFormat == "yaml"):
            configFileName = create_yaml_from_data(experiment, trialNum)
            paramNames = get_config_paramNames_yaml(f'configFiles/{configFileName}')
        else:
            configFileName = create_config_from_data(experiment, trialNum)
            paramNames = get_config_paramNames_ini(f'configFiles/{configFileName}')
    except Exception as err:
        raise GladosInternalError(f"Failed to generate config {trialNum} file") from err

    try:
        _run_trial(experiment, f'../configFiles/{configFileName}', trialNum)
    except (TrialTimeoutError, InternalTrialFailedError) as err:
        _record_trial_failure(experiment, trialNum, err)
        return None

    if experiment.has_extra_files() and experiment.trialExtraFile != None:
        try:
            _add_to_output_batch(f"trial{trialNum}/" + experiment.trialExtraFile, trialNum)
        except FileHandlingError as err:
            _record_trial_failure(experiment, trialNum, err)
            return None

    try:
        lineToGet = experiment.trialResultLineNumber
        output = _get_line_n_of_trial_results_csv(lineToGet, f"trial{trialNum}/" + experiment.trialResult)
    except GladosUserError as err:
        _record_trial_failure(experiment, trialNum, err)
        return None

    # return the object that will be written to a row
    if(experiment.configFileFormat == "yaml"):
        ordered_configs = get_configs_ordered_yaml(f'configFiles/{trialNum}.yaml', paramNames)
    else:
        ordered_configs = get_configs_ordered_ini(f'configFiles/{trialNum}.ini', paramNames)
    return [trialNum] + output + ordered_configs


def merge_partial_result_contents(partials: "list"):
    """Merge shard partial results CSVs into (rows, header).

    Each partial is a dict with a "results" CSV string of `header + rows` (the
    header line -- starting with "Experiment Run" -- is present only when that
    shard had at least one successful trial). Returns every data row sorted by
    trial number and a single header (None when no shard produced any rows). This
    is the pure core of the finalize merge, kept dependency-free for testing.
    """
    header = None
    rows = []
    for partial in partials:
        lines = list(csv.reader(io.StringIO(partial.get('results', ''))))
        if not lines:
            continue
        if lines[0] and lines[0][0] == "Experiment Run":
            if header is None:
                header = lines[0]
            dataLines = lines[1:]
        else:
            dataLines = lines
        for line in dataLines:
            if line:
                rows.append(line)
    rows.sort(key=lambda r: int(r[0]))
    return rows, header


def shard_trial_nums(totalExperimentRuns: int, shardIndex: int, numShards: int):
    """The trial indices this shard owns: every trialNum where n % numShards == shardIndex.

    Because trial N maps to config N deterministically on every pod, this
    round-robin partition assigns each trial to exactly one shard with no
    cross-pod coordination. Extra shards (numShards > totalExperimentRuns) simply
    get an empty list.
    """
    if numShards < 1:
        numShards = 1
    return [n for n in range(0, totalExperimentRuns) if n % numShards == shardIndex]


def conduct_experiment(experiment: ExperimentData, shardIndex: int = 0, numShards: int = 1):
    """
    Call this function when inside the experiment folder!

    Runs only this shard's subset of trials and writes a *partial* results.csv
    (this shard's header + its rows). The finalize job merges every shard's
    partial into the single aggregated results.csv.
    """
    os.mkdir('configFiles')
    trialNums = shard_trial_nums(experiment.totalExperimentRuns, shardIndex, numShards)
    explogger.info(f"Running Experiment {experiment.expId} shard {shardIndex}/{numShards}")
    explogger.info(f"This shard runs {len(trialNums)} of {experiment.totalExperimentRuns} trials: {trialNums}")

    results = []

    # Only shard 0 stamps the experiment start time, to avoid redundant/racing writes.
    if shardIndex == 0:
        update_exp_value(experiment.expId, "startedAtEpochMillis", int(time.time() * 1000))

    with ProcessPoolExecutor() as executor:
        futures = [executor.submit(_run_trial_wrapper, experiment, trialNum) for trialNum in trialNums]
        for future in as_completed(futures):
            try:
                row = future.result()
                if row is not None:
                    results.append(row)
                    # Atomic $inc so concurrent shards sum correctly (see increment_exp_value).
                    experiment.passes += 1
                    increment_exp_value(experiment.expId, 'passes', 1)
            except Exception as e:
                explogger.error(f"Task failed with exception: {e}")

    _write_partial_results(experiment, results)
    explogger.info(f"Finished running shard {shardIndex} trials")


def _write_partial_results(experiment: ExperimentData, results: "list"):
    """Write this shard's partial results.csv: its header (if it had any success) + its rows."""
    results.sort(key=lambda x: x[0])
    with open('results.csv', 'w', encoding="utf8") as expResults:
        writer = csv.writer(expResults)
        header = _build_results_header(experiment, results)
        if header is not None:
            writer.writerow(header)
        writer.writerows(results)


def _build_results_header(experiment: ExperimentData, sortedResults: "list"):
    """Build the results header from this shard's lowest-indexed successful trial.

    Returns None if the shard produced no successful trials (empty partial). The
    user's output columns and hyperparameter names are identical across trials,
    so any completed trial in the shard yields a valid header; the finalize merge
    keeps a single one.
    """
    if not sortedResults:
        return None
    firstTrial = sortedResults[0][0]
    if(experiment.configFileFormat == "yaml"):
        paramNames = get_config_paramNames_yaml(f'configFiles/{firstTrial}.yaml')
    else:
        paramNames = get_config_paramNames_ini(f'configFiles/{firstTrial}.ini')
    csvHeader = _get_line_n_of_trial_results_csv(0, f"trial{firstTrial}/" + experiment.trialResult)
    return ["Experiment Run"] + csvHeader + paramNames


def _record_trial_failure(experiment: ExperimentData, trialNum: int, err: BaseException):
    """Record a failed trial: log it and atomically increment the shared fail counter.

    The old code special-cased trial 0 to abort the whole experiment, but that
    path was already dead in the ProcessPoolExecutor design (the exception was
    swallowed by the pool's result loop) and cannot cheaply coordinate an abort
    across shards, so failures are now uniformly counted without a row.
    """
    if isinstance(err, TrialTimeoutError):
        explogger.error(f"Trial#{trialNum} timed out")
    else:
        explogger.error(f"Trial#{trialNum} encountered an error")
    explogger.exception(err)
    experiment.fails += 1
    increment_exp_value(experiment.expId, 'fails', 1)
