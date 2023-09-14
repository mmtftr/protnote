import logging
from src.utils.data import (
    read_pickle,
    load_model_weights,
    read_yaml,
    create_ordered_tensor,
)
from src.data.datasets import ProteinDataset, create_multiple_loaders
from src.models.ProTCLTrainer import ProTCLTrainer
from src.models.ProTCL import ProTCL
from src.utils.evaluation import EvalMetrics
from src.utils.models import count_parameters_by_layer
import numpy as np
import torch
import wandb
import os
import datetime
import argparse
import json
import random
import time
from src.utils.models import (
    load_model_and_tokenizer,
    tokenize_inputs,
    get_embeddings_from_tokens,
)
from torch.utils.data import DataLoader, TensorDataset

# Set the TOKENIZERS_PARALLELISM environment variable to False
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Argument parser setup
parser = argparse.ArgumentParser(description="Train and/or Test the ProTCL model.")
parser.add_argument(
    "--use-wandb",
    action="store_true",
    default=False,
    help="Use Weights & Biases for logging. Default is False.",
)
parser.add_argument(
    "--load-model",
    type=str,
    default=None,
    help="(Relative) path to the model to be loaded. If not provided, a new model will be initialized.",
)
parser.add_argument(
    "--config",
    type=str,
    default="configs/base_config.yaml",
    help="(Relative) path to the configuration file.",
)

# TODO: Make Optimization metric and normalize probabilities part of arguments
args = parser.parse_args()


# Get the root path from the environment variable; default to current directory if ROOT_PATH is not set
ROOT_PATH = os.environ.get("ROOT_PATH", ".")

# Load the configuration file
config = read_yaml(os.path.join(ROOT_PATH, args.config))

# Extract the parameters and paths from the (possibly overidden) config file
params = config["params"]
paths = {
    key: os.path.join(ROOT_PATH, value)
    for key, value in config["relative_paths"].items()
}

# Set the timezone for the entire Python environment
os.environ["TZ"] = "US/Pacific"
time.tzset()
timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S %Z").strip()

# Initialize logging
# TODO: Find a way to give W&B access to the log file
log_dir = os.path.join(ROOT_PATH, "logs")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
full_log_path = os.path.join(log_dir, f"{timestamp}_train_{args.name}.log")
logging.basicConfig(
    filename=full_log_path,
    filemode="w",
    format="%(asctime)s %(levelname)-4s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S %Z",
)

logger = logging.getLogger()
print(f"Logging to {full_log_path}...")

# Initialize new run
logger.info(f"################## {timestamp} RUNNING train.py ##################")

# Initialize W&B, if using
if args.use_wandb:
    wandb.init(
        project="protein-functions",
        name=f"{args.name}_{timestamp}",
        config={**params, **vars(args)},
    )

# Log the configuration and arguments
logger.info(f"Configuration: {config}")
logger.info(f"Arguments: {args}")

# Use GPU if available
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
logging.info(f"Using device: {device}")

# Load datasets from config file paths; the same vocabulary is used for all datasets
common_paths = {
    "amino_acid_vocabulary_path": paths["AMINO_ACID_VOCAB_PATH"],
    "label_vocabulary_path": paths["GO_LABEL_VOCAB_PATH"],
    "sequence_id_vocabulary_path": paths["SEQUENCE_ID_VOCAB_PATH"],
    "sequence_id_map_path": paths["SEQUENCE_ID_MAP_PATH"],
}
paths_list = [
    {**common_paths, "data_path": paths[key]}
    for key in ["TRAIN_DATA_PATH", "VAL_DATA_PATH", "TEST_DATA_PATH"]
]

test_dataset = ProteinDataset.create_multiple_datasets(paths_list)[0]

# Define data loaders
test_loader = create_multiple_loaders(
    [test_dataset],
    [params["TEST_BATCH_SIZE"]],
    num_workers=params["NUM_WORKERS"],
    pin_memory=True,
)[0]


# Load map from alphanumeric sequence ID's to integer sequence ID's
sequence_id_map = read_pickle(paths["SEQUENCE_ID_MAP_PATH"])

# Load sequence embeddings
sequence_embedding_matrix = sequence_encoder = None
if not params["TRAIN_SEQUENCE_ENCODER"]:
    # TODO: Rather than loading from file, create from ProteInfer itself (slower at startup, but more flexible)
    sequence_embedding_matrix = create_ordered_tensor(
        paths["SEQUENCE_EMBEDDING_PATH"],
        test_dataset.sequence_id2int,  # TODO: Not sure if this should be from test_dataset
        params["PROTEIN_EMBEDDING_DIM"],
        device,
    )
    logger.info("Loaded sequence embeddings.")

# Load label embeddings or label encoder
label_embedding_matrix = label_encoder = tokenized_labels_dataloader = None
if not params["TRAIN_LABEL_ENCODER"]:
    # TODO: Rather than loading from file, create from the model itself (slower at startup, but more flexible)
    label_embedding_matrix = create_ordered_tensor(
        paths["LABEL_EMBEDDING_PATH"],
        test_dataset.label2int,  # TODO: Not sure if this should be from test_dataset
        params["LABEL_EMBEDDING_DIM"],
        device,
    )
    logger.info("Loaded label embeddings.")
# Otherwise, load the pre-tokenized labels
else:
    # Load the go annotations (include free text) from data file
    annotations = read_pickle(paths["GO_DESCRIPTIONS_PATH"])

    # Filter the annotations df to be only the labels in label_vocab. In annotations, the go id is the index
    annotations = annotations[annotations.index.isin(test_dataset.label_vocabulary)]

    # Add a new column 'numeric_id' to the dataframe based on the id_map
    annotations["numeric_id"] = annotations.index.map(test_dataset.label2int)

    # Sort the dataframe by 'numeric_id'
    annotations_sorted = annotations.sort_values(by="numeric_id")

    # Extract the "label" column as a list
    sorted_labels = annotations_sorted["label"].tolist()

    checkpoint = params["PUBMEDBERT_CHECKPOINT"]

    # Load the model and tokenizer, then tokenize the labels
    label_tokenizer, label_encoder = load_model_and_tokenizer(
        checkpoint, freeze_weights=not params["TRAIN_LABEL_ENCODER"]
    )
    label_encoder = label_encoder.to(device)
    model_inputs = tokenize_inputs(label_tokenizer, sorted_labels)

    # Move the tensors to GPU if available
    model_inputs = {name: tensor.to(device) for name, tensor in model_inputs.items()}

    # Create a DataLoader to iterate over the tokenized labels in batches
    # TODO: Move this other batch size to arguments as LABEL_BATCH_SIZE
    tokenized_labels_dataloader = DataLoader(
        TensorDataset(*model_inputs.values()), batch_size=500
    )

# Seed everything so we don't go crazy
random.seed(params["SEED"])
np.random.seed(params["SEED"])
torch.manual_seed(params["SEED"])
if device == "cuda":
    torch.cuda.manual_seed_all(params["SEED"])

# Initialize the models

# TODO: Initialize ProteInfer and PubMedBERT here as well as the ensemble (ProTCL), which should take the other two as optional arguments

model = ProTCL(
    protein_embedding_dim=params["PROTEIN_EMBEDDING_DIM"],
    label_embedding_dim=params["LABEL_EMBEDDING_DIM"],
    latent_dim=params["LATENT_EMBEDDING_DIM"],
    temperature=params["TEMPERATURE"],
    label_encoder=label_encoder,
    tokenized_labels_dataloader=tokenized_labels_dataloader,
    sequence_encoder=sequence_encoder,
    sequence_embedding_matrix=sequence_embedding_matrix,
    label_embedding_matrix=label_embedding_matrix,
    train_projection_head=params["TRAIN_PROJECTION_HEAD"],
    train_label_embeddings=params["TRAIN_LABEL_EMBEDDING_MATRIX"],
    train_sequence_embeddings=params["TRAIN_SEQUENCE_EMBEDDING_MATRIX"],
    train_label_encoder=params["TRAIN_LABEL_ENCODER"],
    train_sequence_encoder=params["TRAIN_SEQUENCE_ENCODER"],
).to(device)

# Initialize trainer class to handle model training, validation, and testing
Trainer = ProTCLTrainer(
    model=model,
    device=device,
    config=config,
    logger=logger,
    timestamp=timestamp,
    run_name=args.name,
    use_wandb=args.use_wandb,
)

# Log the number of parameters by layer
count_parameters_by_layer(model)

# Load the model weights if --load-model argument is provided
if args.load_model:
    load_model_weights(model, os.path.join(ROOT_PATH, args.load_model))
    logger.info(f"Loading model weights from {args.load_model}...")


####### TESTING LOOP #######
if args.mode in ["test", "both"]:
    logger.info("Starting testing...")
    best_val_th = params["DECISION_TH"]

    # If no decision threshold is provided, find the optimal threshold on the validation set
    if params["DECISION_TH"] is None:
        logger.info("Decision threshold not provided.")

        best_val_th, best_val_score = Trainer.find_optimal_threshold(
            data_loader=test_loader, optimization_metric_name="f1_micro"
        )
    # Evaluate model on test set
    eval_metrics = EvalMetrics(
        num_labels=params["NUM_LABELS"], threshold=best_val_th, device=device
    ).get_metric_collection(type="all")

    final_metrics = Trainer.evaluate(
        data_loader=test_loader, eval_metrics=eval_metrics, testing=True
    )
    logger.info(json.dumps(final_metrics, indent=4))
    logger.info("Testing complete.")

# Close the W&B run
if args.use_wandb:
    # TODO: Check to ensure W&B is logging these test metrics correctly
    wandb.log(final_metrics)
    wandb.finish()

# Clear GPU cache
torch.cuda.empty_cache()

logger.info("################## train.py COMPLETE ##################")