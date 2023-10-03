import torch
import os
import time
import datetime
import logging
from src.utils.data import read_yaml
import sys


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
                config["params"][key] = type(config["params"][key])(value)
            else:
                raise KeyError(
                    f"Key '{key}' not found in the 'params' section of the config."
                )


def get_setup(
    config_path: str,
    run_name: str,
    overrides: list,
    train_path_name: str = None,
    val_path_name: str = None,
    test_paths_names: list = None,
):
    # Get the root path from the environment variable; default to current directory if ROOT_PATH is not set
    ROOT_PATH = os.environ.get("ROOT_PATH", ".")
    AMLT_LOGS_DIR = os.environ.get("AMLT_LOGS_DIR",".")

    # Load the configuration file
    config = read_yaml(os.path.join(ROOT_PATH, config_path))

    if overrides:
        override_config(config, overrides)

    # Extract the parameters and paths from the (possibly overidden) config file
    params = config["params"]
    paths = {
        key: os.path.join(ROOT_PATH, value)
        for key, value in config["relative_paths"].items()
    }

    # Load datasets from config file paths; the same vocabulary is used for all datasets
    common_paths = {
        "vocabularies_dir": paths["VOCABULARIES_DIR"],
        "go_annotations_path": paths['GO_ANNOTATIONS_PATH']
    }

    train_paths_list = (
        [{**common_paths, "data_path": paths[train_path_name], "dataset_type": "train"}]
        if train_path_name is not None
        else []
    )

    val_paths_list = (
        [
            {
                **common_paths,
                "data_path": paths[val_path_name],
                "dataset_type": "validation",
            }
        ]
        if val_path_name is not None
        else []
    )

    test_paths_list = (
        [
            {**common_paths, "data_path": paths[key], "dataset_type": "test"}
            for key in test_paths_names
        ]
        if test_paths_names is not None
        else []
    )

    paths_list = train_paths_list + val_paths_list + test_paths_list

    # Set the timezone for the entire Python environment
    os.environ["TZ"] = "US/Pacific"
    time.tzset()
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S %Z").strip()

    # Initialize logging
    log_dir = os.path.join(ROOT_PATH, "logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    full_log_path = os.path.join(log_dir, f"{timestamp}_train_{run_name}.log")

    # Set up the logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Create a formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-4s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z"
    )

    # Create a file handler and add it to the logger
    file_handler = logging.FileHandler(full_log_path, mode="w")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Create a stream handler (for stdout) and add it to the logger
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    print(f"Logging to {full_log_path} and console...")

    # Use GPU if available
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logging.info(f"Using device: {device}")
    return config, params, paths, paths_list, timestamp, logger, device, ROOT_PATH
