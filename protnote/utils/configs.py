import os
import time
import datetime
import logging
from protnote.utils.data import read_yaml
import sys
from ast import literal_eval
from pathlib import Path


def get_logger():
    # Create a custom logger
    logger = logging.getLogger(__name__)

    # Set the logging level to INFO
    logger.setLevel(logging.INFO)

    # Create a console handler and set its level to INFO
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Create a formatter that includes the current date and time
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Set the formatter for the console handler
    console_handler.setFormatter(formatter)

    # Add the console handler to the logger
    logger.addHandler(console_handler)

    # Example usage
    logger.info("This is an info message.")
    return logger


def try_literal_eval(val):
    try:
        # Attempt to evaluate as a literal
        return literal_eval(val)
    except (ValueError, SyntaxError):
        # If evaluation fails means input is actually a string
        if val == "null":
            return None
        if (val == "false") | (val == "true"):
            return literal_eval(val.title())
        return val


def override_config(config: dict, overrides: list):
    # Process the overrides if provided
    if overrides:
        if len(overrides) % 2 != 0:
            raise ValueError("Overrides must be provided as key-value pairs.")

        # Convert the list to a dictionary
        overrides = {
            overrides[i]: overrides[i + 1] for i in range(0, len(overrides), 2)
        }

        # Update the config with the overrides
        for key, value in overrides.items():
            # Convert value to appropriate type if necessary (e.g., float, int)
            # Here, we're assuming that the provided keys exist in the 'params' section of the config
            if key in config["params"]:
                config["params"][key] = try_literal_eval(value)
            else:
                raise KeyError(
                    f"Key '{key}' not found in the 'params' section of the config."
                )


def generate_label_embedding_path(params: dict, base_label_embedding_path: str):
    """
    Generates the name of the file that caches label embeddings. Needed due to different
    ways of pooling embeddings, different types of go descriptions and other paramters.
    This way we can store different versions/types of label embeddings for caching
    """
    assert params["LABEL_ENCODER_CHECKPOINT"] in [
        "microsoft/biogpt",
        "intfloat/e5-large-v2",
        "intfloat/multilingual-e5-large-instruct",
    ], "Model not supported"

    MODEL_NAME_2_NICKNAME = {
        "microsoft/biogpt": "BioGPT",
        "intfloat/e5-large-v2": "E5",
        "intfloat/multilingual-e5-large-instruct": "E5_multiling_inst",
    }

    label_embedding_path = base_label_embedding_path.split("/")
    temp = label_embedding_path[-1].split(".")

    base_model = temp[0].split("_")
    base_model = "_".join(
        [base_model[0]]
        + [MODEL_NAME_2_NICKNAME[params["LABEL_ENCODER_CHECKPOINT"]]]
        + base_model[1:]
    )

    label_embedding_path[-1] = (
        base_model + "_" + params["LABEL_EMBEDDING_POOLING_METHOD"] + "." + temp[1]
    )

    label_embedding_path = "/".join(label_embedding_path)
    return label_embedding_path


def get_setup(
    config_path: str,
    run_name: str,
    overrides: list,
    train_path_name: str = None,
    val_path_name: str = None,
    test_paths_names: list = None,
    annotations_path_name: str = None,
    base_label_embedding_name: str = None,
    amlt: bool = False,
    is_master: bool = True,
):
    # Get the root path from the environment variable; default to current directory if ROOT_PATH is not set
    if amlt:
        ROOT_PATH = os.getcwd()  # Set ROOT_PATH to working directory
        DATA_PATH = os.environ["AMLT_DATA_DIR"]
        OUTPUT_PATH = os.environ["AMLT_OUTPUT_DIR"]
    else:
        ROOT_PATH = str(Path(os.path.dirname(__file__)).parents[1])
        print(ROOT_PATH)
        DATA_PATH = os.path.join(ROOT_PATH, "data")
        OUTPUT_PATH = os.path.join(ROOT_PATH, "outputs")
        if not os.path.exists(OUTPUT_PATH) and is_master:
            os.makedirs(OUTPUT_PATH)

    # Load the configuration file and override the parameters if provided
    config = read_yaml(os.path.join(ROOT_PATH, config_path))
    if overrides:
        override_config(config, overrides)

    # Extract the model parameters from the (possibly overidden) config file
    params = config["params"]

    # Extract the fixed ProteInfer params from the config file
    embed_sequences_params = config["embed_sequences_params"]

    # Prepend the correct path roots
    # Define root paths for each section
    section_paths = {
        "data_paths": DATA_PATH,
        "output_paths": OUTPUT_PATH,
    }
    paths = {
        key: os.path.join(section_paths[section], value)
        for section, section_values in config["paths"].items()
        for key, value in section_values.items()
    }

    train_paths_list = (
        [
            {
                "data_path": paths[train_path_name],
                "dataset_type": "train",
                "annotations_path": paths[annotations_path_name],
                "vocabularies_dir": paths["VOCABULARIES_DIR"],
            }
        ]
        if train_path_name is not None
        else []
    )

    val_paths_list = (
        [
            {
                "data_path": paths[val_path_name],
                "dataset_type": "validation",
                "annotations_path": paths[annotations_path_name],
                "vocabularies_dir": paths["VOCABULARIES_DIR"],
            }
        ]
        if val_path_name is not None
        else []
    )

    test_paths_list = (
        [
            {
                "data_path": paths[key],
                "dataset_type": "test",
                "annotations_path": paths[annotations_path_name],
                "vocabularies_dir": paths["VOCABULARIES_DIR"],
            }
            for key in test_paths_names
        ]
        if test_paths_names is not None
        else []
    )

    dataset_paths = {
        "train": train_paths_list,
        "validation": val_paths_list,
        "test": test_paths_list,
    }

    # Set the timezone for the entire Python environment
    os.environ["TZ"] = "US/Pacific"
    time.tzset()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S %Z").strip()

    # Initialize logging
    log_dir = paths["LOG_DIR"]
    if not os.path.exists(log_dir) and is_master:
        try:
            os.makedirs(log_dir)
        except FileExistsError:
            print(f"Log directory {log_dir} already exists. is_master={is_master}")
            pass
    full_log_path = os.path.join(log_dir, f"{timestamp}_{run_name}.log")

    # Initialize the logger for all processes
    logger = logging.getLogger()

    # Only log to file and console if this is the main process
    if is_master:
        # Set the logging level (default for other processes is WARNING)
        logger.setLevel(logging.INFO)

        # Create a formatter
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-4s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S %Z",
        )

        # Create a file handler and add it to the logger
        file_handler = logging.FileHandler(full_log_path, mode="w")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Create a stream handler (for stdout) and add it to the logger
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        logger.info(f"Logging to {full_log_path} and console...")
    else:
        # Set the logger level to an unreachable level, effectively disabling it
        logger.setLevel(logging.CRITICAL + 1)

    # Generate embeddings
    label_embedding_path = generate_label_embedding_path(
        params=params, base_label_embedding_path=paths[base_label_embedding_name]
    )

    # Return a dictionary
    return {
        "params": params,
        "embed_sequences_params": embed_sequences_params,
        "paths": paths,
        "dataset_paths": dataset_paths,
        "timestamp": timestamp,
        "logger": logger,
        "ROOT_PATH": ROOT_PATH,
        "DATA_PATH": DATA_PATH,
        "OUTPUT_PATH": OUTPUT_PATH,
        "LABEL_EMBEDDING_PATH": label_embedding_path,
    }


def get_project_root():
    """Dynamically determine the project root."""
    return Path(__file__).resolve().parent.parent.parent  # Adjust based on the folder structure

def update_config_paths(config, project_root):
    # Prepend project_root / 'data' to all paths in the 'data' section
    for key, value in config['paths'].get('data_paths', {}).items():
        config['paths']['data_paths'][key] = project_root / 'data' / value

    for key, value in config['paths'].get('output_paths', {}).items():
        config['paths']['output_paths'][key] = project_root / 'outputs' / value

    return config

def load_config(config_file:str = 'base_config.yaml'):
    """Load the environment variables and YAML configuration file."""
    project_root = get_project_root()

    # Load the YAML configuration file from the project root
    config_file = project_root / 'configs' / config_file
    config = update_config_paths(read_yaml(config_file),project_root) 

    return config, project_root

def construct_absolute_paths(dir:str,files:list)->list:
    return [Path(dir) / file for file in files]
