from typing import Union
from datasets import Dataset, DatasetDict
from models.hf_models.tokenization_hf_cehrbert import CehrBertTokenizer
from data_generators.hf_data_generator.hf_dataset_mapping import (
    MedToCehrBertDatasetMapping,
    SortPatientSequenceMapping,
    HFTokenizationMapping,
    HFFineTuningMapping
)
from runner.hf_runner_argument_dataclass import DataTrainingArguments

CEHRBERT_COLUMNS = [
    'concept_ids', 'ages', 'dates', 'visit_segments',
    'visit_concept_orders', 'concept_values', 'concept_value_masks',
    'mlm_skip_values'
]

TRANSFORMER_COLUMNS = ['input_ids', 'labels']

FINETUNING_COLUMNS = ['age_at_index', 'classifier_label', 'index_date', 'person_id']


def convert_meds_to_cehrbert(
        meds_dataset: Union[Dataset, DatasetDict],
        data_args: DataTrainingArguments
) -> Dataset:
    if data_args.is_data_in_med:
        med_to_cehrbert_mapping = MedToCehrBertDatasetMapping(
            data_args
        )
        if data_args.streaming:
            if isinstance(meds_dataset, DatasetDict):
                cehrbert_dataset = DatasetDict()
                for split in meds_dataset.keys():
                    cehrbert_dataset[split] = meds_dataset[split].map(
                        med_to_cehrbert_mapping.batch_transform,
                        batched=True,
                        batch_size=data_args.preprocessing_batch_size
                    ).filter(
                        lambda records: [r >= 1 for r in records['num_of_visits']],
                        batched=True,
                        batch_size=data_args.preprocessing_batch_size
                    )
            else:
                cehrbert_dataset = meds_dataset.map(
                    med_to_cehrbert_mapping.batch_transform,
                    batched=True,
                    batch_size=data_args.preprocessing_batch_size
                ).filter(
                    lambda records: [r >= 1 for r in records['num_of_visits']],
                    batched=True,
                    batch_size=data_args.preprocessing_batch_size
                )
        else:
            cehrbert_dataset = meds_dataset.map(
                med_to_cehrbert_mapping.batch_transform,
                batched=True,
                batch_size=data_args.preprocessing_batch_size,
                num_proc=data_args.preprocessing_num_workers,
            ).filter(
                lambda records: [r >= 1 for r in records['num_of_visits']],
                num_proc=data_args.preprocessing_num_workers,
                batched=True,
                batch_size=data_args.preprocessing_batch_size
            )
        return cehrbert_dataset
    else:
        raise RuntimeError(f"is_data_in_med is not set to True in DataTrainingArguments: {data_args}")


def create_cehrbert_pretraining_dataset(
        dataset: Union[Dataset, DatasetDict],
        concept_tokenizer: CehrBertTokenizer,
        data_args: DataTrainingArguments
) -> Dataset:
    required_columns = TRANSFORMER_COLUMNS + CEHRBERT_COLUMNS
    mapping_functions = [
        SortPatientSequenceMapping(),
        HFTokenizationMapping(concept_tokenizer, True)
    ]
    for mapping_function in mapping_functions:
        if data_args.streaming:
            if isinstance(dataset, DatasetDict):
                for dataset_name in dataset.keys():
                    dataset[dataset_name] = (
                        dataset[dataset_name].map(
                            mapping_function.batch_transform,
                            batched=True,
                            batch_size=data_args.preprocessing_batch_size
                        )
                    )
            else:
                dataset = dataset.map(
                    mapping_function.batch_transform,
                    batched=True,
                    batch_size=data_args.preprocessing_batch_size
                )
        else:
            dataset = dataset.map(
                mapping_function.batch_transform,
                num_proc=data_args.preprocessing_num_workers,
                batched=True,
                batch_size=data_args.preprocessing_batch_size
            )

    if isinstance(dataset, DatasetDict):
        all_columns = dataset['train'].column_names
    else:
        all_columns = dataset.column_names

    if not data_args.streaming:
        columns_to_remove = [_ for _ in all_columns if _ not in required_columns]
        dataset = dataset.remove_columns(columns_to_remove)

    return dataset


def create_cehrbert_finetuning_dataset(
        dataset: Union[Dataset, DatasetDict],
        concept_tokenizer: CehrBertTokenizer,
        data_args: DataTrainingArguments
) -> Dataset:
    required_columns = TRANSFORMER_COLUMNS + CEHRBERT_COLUMNS + FINETUNING_COLUMNS
    mapping_functions = [
        SortPatientSequenceMapping(),
        HFTokenizationMapping(concept_tokenizer, False),
        HFFineTuningMapping()
    ]

    if data_args.is_data_in_med:
        med_to_cehrbert_mapping = MedToCehrBertDatasetMapping(
            data_args
        )
        mapping_functions.insert(0, med_to_cehrbert_mapping)

    for mapping_function in mapping_functions:
        dataset = dataset.map(mapping_function.transform, num_proc=data_args.preprocessing_num_workers)

    if isinstance(dataset, DatasetDict):
        all_columns = dataset['train'].column_names
    else:
        all_columns = dataset.column_names

    columns_to_remove = [_ for _ in all_columns if _ not in required_columns]
    dataset = dataset.remove_columns(columns_to_remove)
    return dataset
