import hashlib
import os
import re
import glob
import sys
from typing import Tuple, Union
from pathlib import Path

import torch
from datasets import load_dataset, Dataset, IterableDataset
from torch.nn import functional as F
from transformers import HfArgumentParser, TrainingArguments, EvalPrediction
from transformers.utils import logging
from transformers.trainer_utils import get_last_checkpoint

from runner.hf_runner_argument_dataclass import ModelArguments, DataTrainingArguments

LOG = logging.get_logger("transformers")


def load_parquet_as_dataset(data_folder, split="train", streaming=False) -> Union[Dataset, IterableDataset]:
    """
    Loads a dataset from Parquet files located within a specified folder into a Hugging Face `datasets.Dataset`.

    This function searches for all `.parquet` files in the specified folder and loads them as a dataset using the
    Hugging Face `datasets` library. It allows specifying a particular dataset split (e.g., 'train', 'test').

    Parameters:
        data_folder (str): The path to the folder containing Parquet files. The function will look for all `.parquet`
                           files within this directory.
        split (str, optional): The split of the dataset to load. Default is 'train'. This can typically be 'train',
                               'test', or 'validation', depending on how you wish to use the dataset.
        streaming (bool, optional): Indicate whether we want to stream the dataset

    Returns:
        datasets.Dataset: A dataset object containing the data from all Parquet files found in the specified folder.
                          This dataset is compatible with the Hugging Face `datasets` library and can be used directly
                          for model training or evaluation.

    Example:
        >>> data_folder = './data/train_data'
        >>> dataset = load_parquet_as_dataset(data_folder, split='train')
        >>> print(dataset.shape)
        (number_of_rows, number_of_columns)

    Note:
        The function assumes that all Parquet files in the specified folder belong to the same split and schema. If
        files differ in schema or are meant to represent different splits, separate calls and directory structuring
        are advised.
    """
    data_abspath = os.path.abspath(data_folder)
    data_files = glob.glob(os.path.join(data_abspath, "*.parquet"))
    dataset = load_dataset('parquet', data_files=data_files, split=split, streaming=streaming)
    return dataset


def get_last_hf_checkpoint(training_args):
    """
    Retrieves the path to the last saved checkpoint from the specified output directory,
    if it exists and conditions permit resuming training from that checkpoint.

    This function checks if an output directory contains any previously saved checkpoints and
    returns the path to the last checkpoint if found. It raises an error if the output directory
    is not empty and overwriting is not enabled, unless explicitly handled by the user to resume
    training or to start afresh by setting the appropriate training arguments.

    Parameters:
        training_args (TrainingArguments): An object containing training configuration parameters, including:
            - output_dir (str): The path to the directory where training outputs are saved.
            - do_train (bool): Whether training is to be performed. If False, the function will not check for checkpoints.
            - overwrite_output_dir (bool): If True, allows overwriting files in `output_dir`.
            - resume_from_checkpoint (str or None): Path to the checkpoint from which training should be resumed.

    Returns:
        str or None: The path to the last checkpoint if a valid checkpoint exists and training is set to resume.
        Returns None if no checkpoints are found or if resuming from checkpoints is not enabled.

    Raises:
        ValueError: If the output directory exists, is not empty, and `overwrite_output_dir` is False,
                    indicating a potential unintended overwriting of training results.

    Example:
        >>> training_args = TrainingArguments(
        ...     output_dir='./results',
        ...     do_train=True,
        ...     overwrite_output_dir=False,
        ...     resume_from_checkpoint=None
        ... )
        >>> last_checkpoint = get_last_hf_checkpoint(training_args)
        >>> print(last_checkpoint)
        '/absolute/path/to/results/checkpoint-500'

    Note:
        If `last_checkpoint` is detected and `resume_from_checkpoint` is None, training will automatically
        resume from the last checkpoint unless instructed otherwise via the `overwrite_output_dir` flag or
        by changing the output directory.
    """
    last_checkpoint = None
    output_dir_abspath = os.path.abspath(training_args.output_dir)
    if os.path.isdir(output_dir_abspath) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(output_dir_abspath)
        if last_checkpoint is None and len([_ for _ in os.listdir(output_dir_abspath) if os.path.isdir(_)]) > 0:
            raise ValueError(
                f"Output directory ({output_dir_abspath}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            LOG.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )
    return last_checkpoint


def md5(to_hash: str, encoding: str = "utf-8") -> str:
    try:
        return hashlib.md5(to_hash.encode(encoding), usedforsecurity=False).hexdigest()
    except TypeError:
        return hashlib.md5(to_hash.encode(encoding)).hexdigest()


def generate_prepared_ds_path(data_args, model_args) -> Path:
    """
   Generates a unique path for storing or retrieving a prepared dataset based on the specified arguments.

   This function constructs a path using a hash value that encapsulates certain attributes from the data
   and model arguments. The hash ensures that the path is unique to the specific combination of model settings
   and data configuration, helping in caching or versioning of processed datasets.

   Parameters:
       data_args (DataTrainingArguments): An object containing arguments related to the training data, such
           as the data folder and validation split percentage.
       model_args (ModelArguments): An object containing arguments specific to the model configuration, such
           as the maximum position embeddings and tokenizer path.

   Returns:
       Path: A pathlib.Path object representing the unique path for the prepared dataset. This path includes
           the base directory specified in `data_args.dataset_prepared_path` combined with a unique hash derived
           from other input arguments.

   Example:
       >>> data_args = DataTrainingArguments(data_folder='./data', validation_split_percentage=10,
       ...                                   dataset_prepared_path='./prepared')
       >>> model_args = ModelArguments(max_position_embeddings=512, tokenizer_name_or_path='bert-base-uncased')
       >>> path = generate_prepared_ds_path(data_args, model_args)
       >>> print(path)
       PosixPath('/absolute/path/to/prepared/1234567890abcdef1234567890abcdef')

   Note:
       The hash is generated from the string representation of the maximum position embeddings, the absolute
       paths of the data folder and tokenizer, and the validation split percentage. If `validation_split_percentage`
       is None or zero, it is omitted from the hash to maintain consistency.
    """
    concatenated_str = (
            str(model_args.max_position_embeddings) +
            "|" + os.path.abspath(data_args.data_folder) +
            "|" + os.path.abspath(model_args.tokenizer_name_or_path) +
            "|" + (str(data_args.validation_split_percentage) if data_args.validation_split_percentage else "") +
            "|" + f"test_eval_ratio={str(data_args.test_eval_ratio)}" +
            "|" + f"split_by_patient={str(data_args.split_by_patient)}" +
            "|" + f"chronological_split={str(data_args.chronological_split)}"
    )
    basename = os.path.basename(data_args.data_folder)
    cleaned_basename = re.sub(r'[^a-zA-Z0-9_]', '', basename)
    LOG.info(f"concatenated_str: {concatenated_str}")
    ds_hash = f"{cleaned_basename}_{str(md5(concatenated_str))}"
    LOG.info(f"ds_hash: {ds_hash}")
    prepared_ds_path = (
            Path(os.path.abspath(data_args.dataset_prepared_path)) / ds_hash
    )
    return prepared_ds_path


def parse_runner_args() -> Tuple[DataTrainingArguments, ModelArguments, TrainingArguments]:
    """
   Parses command line arguments provided to a script for training a model using the Hugging Face library.

   This function uses HfArgumentParser to parse arguments from either command line directly or from configuration files
   in JSON or YAML format. The arguments are expected to belong to three categories: ModelArguments, DataTrainingArguments,
   and TrainingArguments, each corresponding to specific configurations required for the model training.

   The function checks the system's command line arguments:
   - If there is exactly one argument and it is a JSON file, it parses the JSON file to extract the arguments.
   - If there is exactly one argument and it is a YAML file, it parses the YAML file instead.
   - Otherwise, it assumes arguments are provided directly through the command line and parses them accordingly.

   Returns:
       tuple: A tuple containing three elements:
           - data_args (DataTrainingArguments): Arguments related to data processing and dataset handling.
           - model_args (ModelArguments): Arguments related to model configuration and specifics.
           - training_args (TrainingArguments): Arguments related to the training process, such as learning rate and
             training epochs.

   Raises:
       FileNotFoundError: If the specified JSON or YAML file does not exist.
       json.JSONDecodeError: If there is an error parsing a JSON file.
       yaml.YAMLError: If there is an error parsing a YAML file.
       Exception: For other issues that occur during argument parsing.

   Examples:
       Command line usage might look like this:
       $ python training_script.py --model_name_or_path bert-base-uncased --do_train

       Or using a JSON configuration file:
       $ python training_script.py config.json

       Or using a YAML configuration file:
       $ python training_script.py config.yaml
    """
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    elif len(sys.argv) == 2 and sys.argv[1].endswith(".yaml"):
        model_args, data_args, training_args = parser.parse_yaml_file(yaml_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    return data_args, model_args, training_args


def compute_metrics(eval_pred: EvalPrediction):
    """
    Compute metrics for evaluation predictions.

    Args:
        eval_pred (EvalPrediction): A named tuple containing model outputs and labels.
                                    The `outputs` attribute contains model predictions (logits),
                                    and the `labels` attribute contains the true labels.

    Returns:
        dict: A dictionary containing the computed metrics. Currently, it returns:
              - 'perplexity' (float): The perplexity score computed from the cross-entropy loss.

    This function performs the following steps:
    1. Extracts logits (model predictions) and labels from the input `eval_pred`.
    2. Creates a mask to exclude entries where labels are set to -100 (ignored tokens).
    3. Applies the mask to the logits and labels to get valid (non-ignored) entries.
    4. Converts logits to probabilities using softmax.
    5. Converts valid labels to one-hot encoding.
    6. Computes log probabilities using log softmax for numerical stability.
    7. Calculates cross-entropy loss for valid entries.
    8. Computes and returns perplexity based on the cross-entropy loss.
    """
    outputs, labels = eval_pred
    # Transformers Trainer will remove the loss from the model output
    # We need to take the first entry of the model output, which is logits
    logits = outputs[0]
    # Exclude entries where labels == -100
    mask = labels != -100
    valid_logits = logits[mask]
    valid_labels = labels[mask]

    # Convert logits to probabilities using the numerically stable softmax
    probabilities = F.softmax(valid_logits, dim=1)

    # Prepare labels for valid (non-masked) entries
    # Note: PyTorch can calculate cross-entropy directly from logits,
    # so converting logits to probabilities is unnecessary for loss calculation.
    # However, we will calculate manually to follow the specified steps.

    # Convert labels to one-hot encoding
    labels_one_hot = F.one_hot(valid_labels, num_classes=probabilities.shape[1]).float()

    # Compute log probabilities (log softmax is more numerically stable than log(softmax))
    log_probs = F.log_softmax(valid_logits, dim=1)

    # Compute cross-entropy loss for valid entries
    cross_entropy_loss = -torch.sum(labels_one_hot * log_probs, dim=1)

    # Calculate perplexity
    perplexity = torch.exp(torch.mean(cross_entropy_loss))

    return {"perplexity": perplexity.item()}  # Use .item() to extract the scalar value from the tensor


def get_meds_extension_path(data_args: DataTrainingArguments):
    data_folder = data_args.data_folder
    if data_folder.endswith("\\"):
        data_folder.rstrip("\\")
    basename = os.path.basename(data_folder)
    meds_extension_path = os.path.join(data_args.dataset_prepared_path, f"{basename}_meds_extension")
    return meds_extension_path
