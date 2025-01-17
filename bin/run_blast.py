import os
import warnings
warnings.simplefilter("ignore")
from tqdm import tqdm
import argparse
from protnote.utils.data import read_fasta, generate_vocabularies
from protnote.utils.configs import load_config,construct_absolute_paths, get_project_root
from protnote.models.blast import BlastTopHits
from protnote.utils.data import tqdm_joblib
import pandas as pd
import multiprocessing
from protnote.utils.configs import get_logger
from joblib import Parallel, delayed
import numpy as np


# Load the configuration and project root
config, project_root = load_config()
results_dir = config["paths"]["output_paths"]["RESULTS_DIR"]


def main():
    parser = argparse.ArgumentParser(description="Run BLAST")
    parser.add_argument(
        "--test-data-path",
        type=str,
        required=True,
        help="The test databse of query sequences",
    )
    parser.add_argument(
        "--train-data-path",
        type=str,
        required=False,
        default=config["paths"]["data_paths"]["TRAIN_DATA_PATH"],
        help="The train databse of sequences",
    )

    parser.add_argument(
        "--top-k-hits",
        type=int,
        required=False,
        default=1,
        help="The number of top hits to return per query in decreasing hit_expect order",
    )
    parser.add_argument(
        "--max-evalue",
        type=float,
        required=False,
        default=0.05,
        help="The evalue threshold. Any this with higher evalue than this threshold is omitted from results",
    )
    parser.add_argument(
        "--cache",
        action="store_true",
        default=False,
        help="Whether to cache results if available",
    )

    parser.add_argument(
        "--save-runtime-info",
        action="store_true",
        default=False,
        help="Whether to save runtime information"
    )

    args = parser.parse_args()

    # Suppress all Biopython warnings
    logger = get_logger()

    test_name = args.test_data_path.split("/")[-1].split(".")[0]
    train_name = args.train_data_path.split("/")[-1].split(".")[0]

    raw_results_output_path = results_dir / f"blast_raw_{test_name}_{train_name}_results.tsv"
    parsed_results_output_path = results_dir / f"blast_parsed_{test_name}_{train_name}_results.parquet"
    pivot_parsed_results_output_path = results_dir / f"blast_pivot_parsed_{test_name}_{train_name}_results.parquet"
    runtime_info_output_path = results_dir / f"blast_runtime_info_{test_name}_{train_name}.csv"

    bth = BlastTopHits(
        db_fasta_path=args.train_data_path, queries_fasta_path=args.test_data_path
    )

    if not (os.path.exists(raw_results_output_path) & args.cache):
        bth.run_blast(output_path=raw_results_output_path, top_k_hits=args.top_k_hits)

    # Parse and save processed results
    if not (os.path.exists(parsed_results_output_path) & args.cache):
        parsed_results = bth.parse_results(
            blast_results_path=raw_results_output_path,
            flatten_labels=False,
            transfer_labels=True,
        )
        parsed_results.to_parquet(parsed_results_output_path, index=False)
    else:
        parsed_results = pd.read_parquet(parsed_results_output_path)

    # Format as pivoted dataframe
    logger.info("Pivoting data")
    db_vocab = generate_vocabularies(file_path=args.train_data_path)["label_vocab"]
    label2int = {label: idx for idx, label in enumerate(db_vocab)}

    def record_to_pivot(idx_row):
        _, row = idx_row
        record = [-15.0] * len(db_vocab)
        for l in row["transferred_labels"]:
            record[label2int[l]] = 15.0
        record.insert(0, row["sequence_name"])
        return record

    simplified_results = parsed_results[
        ["sequence_name", "bit_score", "transferred_labels"]
    ]

    pivoting_batch_size = 10_000
    num_pivoting_baches = int(np.ceil(len(simplified_results) / 10_000))
    simplified_results.iterrows()

    for batch in range(num_pivoting_baches):
        logger.info(f"Pivoting batch {batch+1} / {num_pivoting_baches}")

        batch_size = min(
            pivoting_batch_size, len(simplified_results) - (batch) * pivoting_batch_size
        )

        with tqdm_joblib(tqdm(total=batch_size)) as pbar:
            records = Parallel(n_jobs=multiprocessing.cpu_count())(
                delayed(record_to_pivot)(idx_row)
                for idx_row in simplified_results[
                    batch * pivoting_batch_size : (batch + 1) * pivoting_batch_size
                ].iterrows()
            )

        result = pd.DataFrame(records, columns=["sequence_name"] + db_vocab)
        result.set_index("sequence_name", inplace=True)
        result.index.name = None
        result.to_parquet(
            str(pivot_parsed_results_output_path).replace('.parquet','') + f"_batch_{batch}.parquet", index=True
        )

    logger.info(f"Merging batched results.")
    batch_results = []
    for batch in tqdm(range(num_pivoting_baches)):
        batch_results.append(
            pd.read_parquet(str(pivot_parsed_results_output_path).replace('.parquet','') + f"_batch_{batch}.parquet")
        )
    pd.concat(batch_results).to_parquet(pivot_parsed_results_output_path, index=True)

    logger.info(f"Results saved in {pivot_parsed_results_output_path}")
    logger.info(f"Search Duration: {bth.run_duration_seconds}")
    logger.info(f"Parse Duration: {bth.parse_results_duration_seconds}")

    # Save the search and parse duration in a csv file, along with the size of query set
    
    if args.save_runtime_info:
        search_parse_duration = pd.DataFrame(
            {
                "search_duration": [bth.run_duration_seconds],
                "parse_duration": [bth.parse_results_duration_seconds],
                "query_size": [len(read_fasta(args.test_data_path))]
            }
        )
        search_parse_duration.to_csv(runtime_info_output_path, index=False)



if __name__ == "__main__":
    """
    sample usage: 
    
    python run_blast.py --test-data-path data/swissprot/proteinfer_splits/random/test_GO.fasta --train-data-path data/swissprot/proteinfer_splits/random/train_GO.fasta
    

    # List of numbers to iterate over
    numbers=(1 10 100 1000 5000 10000 20000)  # Modify this list with your actual numbers

    # Loop through each number in the list
    for num in "${numbers[@]}"; do
        # Run the python script with the current number in the file path
        python bin/run_blast.py --test-data-path data/swissprot/proteinfer_splits/random/test_${num}_GO.fasta --train-data-path data/swissprot/proteinfer_splits/random/train_GO.fasta --save-runtime-info;
    done

    python bin/run_blast.py --test-data-path data/swissprot/proteinfer_splits/random/test_GO.fasta --train-data-path data/swissprot/proteinfer_splits/random/train_GO.fasta --save-runtime-info;

    




    numbers=(20000)  # Modify this list with your actual numbers

    # Loop through each number in the list
    for num in "${numbers[@]}"; do
        # Run the python script with the current number in the file path
        python bin/run_blast.py --test-data-path data/swissprot/proteinfer_splits/random/test_${num}_GO.fasta --train-data-path data/swissprot/proteinfer_splits/random/train_GO.fasta --save-runtime-info;
    done

    python bin/run_blast.py --test-data-path data/swissprot/proteinfer_splits/random/test_GO.fasta --train-data-path data/swissprot/proteinfer_splits/random/train_GO.fasta --save-runtime-info;
    """
    main()

#!/bin/bash

