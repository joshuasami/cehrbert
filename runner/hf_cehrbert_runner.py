import os
import sys
import glob
import logging
from pathlib import Path
from datasets import load_dataset, load_from_disk
from transformers import HfArgumentParser, TrainingArguments
from typing import Union, Tuple
import hashlib

from runner.hf_runner_argument_dataclass import DataTrainingArguments, ModelArguments
from data_generators.hf_data_generator.hf_dataset_collator import CehrBertDataCollator
from data_generators.hf_data_generator.hf_dataset import create_cehrbert_dataset
from models.hf_models.tokenization_hf_cehrbert import CehrBertTokenizer
from models.hf_models.config import CehrBertConfig
from models.hf_models.hf_cehrbert import CehrBertForPreTraining
from transformers import AutoConfig, Trainer, set_seed
from transformers.trainer_utils import get_last_checkpoint

LOG = logging.getLogger("cehrbert_runner")


def md5(to_hash: str, encoding: str = "utf-8") -> str:
    try:
        return hashlib.md5(to_hash.encode(encoding), usedforsecurity=False).hexdigest()
    except TypeError:
        return hashlib.md5(to_hash.encode(encoding)).hexdigest()


def generate_prepared_ds_path(data_args, model_args) -> Path:
    ds_hash = str(
        md5(
            (
                    str(data_args.max_seq_length)
                    + "|"
                    + data_args.data_folder
                    + "|"
                    + model_args.tokenizer_name_or_path
            )
        )
    )
    prepared_ds_path = (
            Path(data_args.dataset_prepared_path) / ds_hash
    )
    return prepared_ds_path


def load_model_and_tokenizer(data_args, model_args) -> Tuple[CehrBertForPreTraining, CehrBertTokenizer]:
    tokenizer = None
    # Try to load the pretrained tokenizer
    try:
        tokenizer = CehrBertTokenizer.from_pretrained(model_args.tokenizer_name_or_path)
    except Union[EnvironmentError, ValueError] as e:
        LOG.warning(e)

    # If the tokenizer doesn't exist, train it from scratch using the training data
    if not tokenizer:
        data_files = glob.glob(os.path.join(data_args.data_folder, "*.parquet"))
        dataset = load_dataset('parquet', data_files=data_files, split='train')
        tokenizer = CehrBertTokenizer.train_tokenizer(dataset, ['concept_ids'], {})
        tokenizer.save_pretrained(model_args.tokenizer_name_or_path)

    # Try to load the pretrained model
    try:
        model_config = AutoConfig.from_pretrained(model_args.model_name_or_path)
    except Exception as e:
        LOG.warning(e)
        model_config = CehrBertConfig(vocab_size=tokenizer.vocab_size, **model_args.as_dict())

    return CehrBertForPreTraining(model_config), tokenizer


def main():
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    elif len(sys.argv) == 2 and sys.argv[1].endswith(".yaml"):
        model_args, data_args, training_args = parser.parse_yaml_file(yaml_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    model, tokenizer = load_model_and_tokenizer(data_args, model_args)

    prepared_ds_path = generate_prepared_ds_path(data_args, model_args)

    if any(prepared_ds_path.glob("*")):
        LOG.info(f"Loading prepared dataset from disk at {prepared_ds_path}...")
        processed_dataset = load_from_disk(str(prepared_ds_path))
        LOG.info("Prepared dataset loaded from disk...")
    else:
        data_files = glob.glob(os.path.join(data_args.data_folder, "*.parquet"))
        dataset = load_dataset('parquet', data_files=data_files, split='train')
        dataset = dataset.train_test_split(test_size=data_args.validation_split_percentage, seed=training_args.seed)
        processed_dataset = create_cehrbert_dataset(
            dataset=dataset,
            concept_tokenizer=tokenizer,
            max_sequence_length=data_args.max_seq_length,
            num_proc=data_args.preprocessing_num_workers
        )
        processed_dataset.save_to_disk(prepared_ds_path)

    collator = CehrBertDataCollator(tokenizer, data_args.max_seq_length)

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            LOG.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Set seed before initializing model.
    set_seed(training_args.seed)

    processed_dataset.set_format('pt')

    trainer = Trainer(
        model=model,
        data_collator=collator,
        train_dataset=processed_dataset['train'],
        eval_dataset=processed_dataset['test'],
        args=training_args
    )

    checkpoint = None
    if training_args.resume_from_checkpoint is not None:
        checkpoint = training_args.resume_from_checkpoint
    elif last_checkpoint is not None:
        checkpoint = last_checkpoint

    train_result = trainer.train(resume_from_checkpoint=checkpoint)
    trainer.save_model()  # Saves the tokenizer too for easy upload
    metrics = train_result.metrics

    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()


if __name__ == "__main__":
    main()