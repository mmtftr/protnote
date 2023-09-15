import torch
from torch.utils.data import Dataset, DataLoader
from src.utils.data import read_fasta, read_pickle, read_json, get_vocab_mappings
from typing import Optional, Text, Dict
import pandas as pd
import logging
from typing import List
from src.data.collators import collate_variable_sequence_length
from collections import defaultdict


class ProteinDataset(Dataset):
    """
    Dataset class for protein sequences with GO annotations.
    """

    def __init__(self, paths: dict, deduplicate: bool = False):
        """
        paths (dict): Dictionary containing paths to the data and vocabularies.
            data_path (str): Path to the FASTA file containing the protein sequences and corresponding GO annotations
            amino_acid_vocabulary_path (str): Path to the JSON file containing the amino acid vocabulary.
            label_vocabulary_path (str): Path to the JSON file containing the label vocabulary.
            sequence_id_vocabulary_path (str): Path to the JSON file containing the sequence ID vocabulary.
        deduplicate (bool): Whether to remove duplicate sequences (default: False)
        """
        # Error handling: check for missing keys
        required_keys = ["data_path","dataset_type"]
        for key in required_keys:
            if key not in paths:
                raise ValueError(f"Missing required key in paths dictionary: {key}")

        assert paths["dataset_type"] in ["train","validation","test"], "dataset_type must be one of 'train', 'val', or 'test'"

        # Set default values for paths not provided
        self.data_path = paths["data_path"]
        self.dataset_type = paths["dataset_type"]
        self.amino_acid_vocabulary_path = paths.get("amino_acid_vocabulary_path", None)
        self.label_vocabulary_path = paths.get("label_vocabulary_path", None)
        self.sequence_id_vocabulary_path = paths.get(
            "sequence_id_vocabulary_path", None
        )

        # Initialize class variables
        self.data = read_fasta(self.data_path)
        self.max_seq_len = None
        self.amino_acid_vocabulary_size = None
        self.label_vocabulary_size = None
        self.sequence_id_vocabulary_size = None


        if deduplicate:
            logging.info(
                "Removing duplicates, keeping only the first instance of each sequence..."
            )
            # Deduplicate sequences
            self._remove_duplicates()

        # Load vocabularies
        self._process_vocab()

        # Create map from sequence ID to unique integer ID

    def _remove_duplicates(self):
        """
        Remove duplicate sequences from self.data, keeping only the first instance of each sequence
        Use pandas to improve performance
        """
        # Convert self.data to a DataFrame
        df = pd.DataFrame(self.data, columns=["sequence", "labels"])

        # Drop duplicate rows based on the 'sequence' column, keeping the first instance
        df = df.drop_duplicates(subset="sequence", keep="first")

        # Convert the DataFrame back to the list of tuples format
        self.data = list(df.itertuples(index=False, name=None))

    def _process_vocab(self):
        self._process_amino_acid_vocab()
        self._process_label_vocab()
        self._process_sequence_id_vocab()

    def _process_amino_acid_vocab(self):
        if self.amino_acid_vocabulary_path is not None:
            self.amino_acid_vocabulary = read_json(self.amino_acid_vocabulary_path)
        else:
            logging.info("No amino acid vocabulary path given. Generating...")
            self.amino_acid_vocabulary = set()
            for obs in self.data:
                self.amino_acid_vocabulary.update(list(obs[0]))
            self.amino_acid_vocabulary = sorted(list(self.amino_acid_vocabulary))
        self.aminoacid2int, self.int2aminoacid = get_vocab_mappings(
            self.amino_acid_vocabulary
        )

    def _process_label_vocab(self):
        if self.label_vocabulary_path is not None:
            self.label_vocabulary = read_json(self.label_vocabulary_path)
        else:
            logging.info("No label vocabulary path given. Generating...")
            self.label_vocabulary = set()
            for obs in self.data:
                self.label_vocabulary.update(obs[1][1:])
            self.label_vocabulary = sorted(list(self.label_vocabulary))
        self.label2int, self.int2label = get_vocab_mappings(self.label_vocabulary)

    def _process_sequence_id_vocab(self):
        if self.sequence_id_vocabulary_path is not None:
            self.sequence_id_vocabulary = read_json(self.sequence_id_vocabulary_path)
        else:
            logging.info("No sequence id vocabulary path given. Generating...")
            self.sequence_id_vocabulary = set()
            for obs in self.data:
                self.sequence_id_vocabulary.add(obs[1][0])
            self.sequence_id_vocabulary = sorted(list(self.sequence_id_vocabulary))
        self.sequence_id2int, self.int2sequence_id = get_vocab_mappings(
            self.sequence_id_vocabulary
        )

    def get_max_seq_len(self):
        if self.max_seq_len is None:
            self.max_seq_len = max(len(i[0]) for i in self.data)
        return self.max_seq_len
    
    def get_amino_acid_vocabulary_size(self):
        if self.amino_acid_vocabulary_size is None:
            self.amino_acid_vocabulary_size = len(self.amino_acid_vocabulary)
        return self.amino_acid_vocabulary_size
    
    def get_label_vocabulary_size(self):
        if self.label_vocabulary_size is None:
            self.label_vocabulary_size = len(self.label_vocabulary)
        return self.label_vocabulary_size
    
    def get_sequence_id_vocabulary_size(self):
        if self.sequence_id_vocabulary_size is None:
            self.sequence_id_vocabulary_size = len(self.sequence_id_vocabulary)
        return self.sequence_id_vocabulary_size

    def __len__(self) -> int:
        return len(self.data)

    def process_example(self, sequence: str, labels: list[str]) -> tuple:
        sequence_id_alphanumeric, labels = labels[0], labels[1:]

        # Get the integer sequence ID and convert to tensor
        sequence_id_numeric = torch.tensor(
            self.sequence_id2int[sequence_id_alphanumeric], dtype=torch.long
        )

        # Convert the sequence and labels to integers
        amino_acid_ints = torch.tensor(
            [self.aminoacid2int[aa] for aa in sequence], dtype=torch.long
        )

        labels_ints = torch.tensor(
            [self.label2int[l] for l in labels], dtype=torch.long
        )

        # Get the length of the sequence
        sequence_length = torch.tensor(len(amino_acid_ints))

        # Get multi-hot encoding of sequence and labels
        sequence_onehots = torch.nn.functional.one_hot(
            amino_acid_ints, num_classes=self.get_amino_acid_vocabulary_size()
        ).permute(1, 0)
        label_multihots = torch.nn.functional.one_hot(
            labels_ints, num_classes=self.get_label_vocabulary_size()
        ).sum(dim=0)

        # Return the integer id, the one-hod sequence, the multi-hot sequence, and the sequence length
        return sequence_id_numeric, sequence_onehots, label_multihots, sequence_length

    def __getitem__(self, idx) -> tuple:
        sequence, labels = self.data[idx]
        return self.process_example(sequence, labels)

    @classmethod
    def create_multiple_datasets(
        cls, paths_list: List[Dict[str, str]], deduplicate: bool = False
    ) -> List[Dataset]:
        """
        paths_list (List[Dict[str, str]]): List of dictionaries, each containing paths to the data and vocabularies.
        Deduplicate (bool): Whether to remove duplicate sequences (default: False). Applies to all datasets in paths_list.
        """
        datasets = defaultdict(list)
        for paths in paths_list:
            datasets[paths['dataset_type']].append(cls(paths, deduplicate=deduplicate))
        return datasets


def set_padding_to_sentinel(
    padded_representations: torch.Tensor,
    sequence_lengths: torch.Tensor,
    sentinel: float,
) -> torch.Tensor:
    """
    Set the padding values in the input tensor to the sentinel value.

    Parameters:
        padded_representations (torch.Tensor): The input tensor of shape (batch_size, dim, max_sequence_length)
        sequence_lengths (torch.Tensor): 1D tensor containing original sequence lengths for each sequence in the batch
        sentinel (float): The value to set the padding to

    Returns:
        torch.Tensor: Tensor with padding values set to sentinel
    """

    # Get the shape of the input tensor
    batch_size, dim, max_sequence_length = padded_representations.shape

    # Get the device of the input tensor
    device = padded_representations.device

    # Create a mask that identifies padding, ensuring it's on the same device
    mask = torch.arange(max_sequence_length, device=device).expand(
        batch_size, max_sequence_length
    ) >= sequence_lengths.unsqueeze(1).to(device)

    # Expand the mask to cover the 'dim' dimension
    mask = mask.unsqueeze(1).expand(-1, dim, -1)

    # Use the mask to set the padding values to sentinel
    padded_representations[mask] = sentinel

    return padded_representations


def create_multiple_loaders(
    datasets: dict,
    params: dict,
    num_workers: int = 2,
    pin_memory: bool = True,
) -> List[DataLoader]:

    loaders = defaultdict(list)
    for dataset_type in datasets.keys():
        for dataset in datasets[dataset_type]:
            loader = torch.utils.data.DataLoader(
                dataset,
                batch_size=params[f"{dataset_type.upper()}_BATCH_SIZE"],
                shuffle=True if dataset_type == "train" else False, #only shuffle train. No need to shuffle val or test.
                collate_fn=collate_variable_sequence_length,
                num_workers=num_workers,
                pin_memory=pin_memory,
            )
            loaders[dataset_type].append(loader)

    return loaders
