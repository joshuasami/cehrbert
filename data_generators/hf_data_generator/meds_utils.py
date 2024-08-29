import os
import re
import collections
import functools
from typing import Dict, List, Optional, Union, Tuple
from datetime import datetime
from dateutil.relativedelta import relativedelta

import meds_reader
import numpy as np
import pandas as pd

from runner.hf_runner_argument_dataclass import DataTrainingArguments
from data_generators.hf_data_generator.hf_dataset_mapping import birth_codes
from med_extension.schema_extension import CehrBertPatient, Visit, Event

from datasets import Dataset, DatasetDict, IterableDataset, Split

UNKNOWN_VALUE = "Unknown"
DEFAULT_ED_CONCEPT_ID = "9203"
DEFAULT_OUTPATIENT_CONCEPT_ID = "9202"
DEFAULT_INPATIENT_CONCEPT_ID = "9201"
MEDS_SPLIT_DATA_SPLIT_MAPPING = {"train": Split.TRAIN, "tuning": Split.VALIDATION, "held_out": Split.TEST}


def get_patient_split(meds_reader_db_path: str) -> Dict[str, List[int]]:
    patient_split = pd.read_parquet(os.path.join(meds_reader_db_path, "metadata/patient_splits.parquet"))
    result = {
        str(group): records["patient_id"].tolist()
        for group, records in patient_split.groupby("split")
    }
    return result


class PatientBlock:
    def __init__(
            self,
            events: List[meds_reader.Event],
            visit_id: int
    ):
        self.visit_id = visit_id
        self.events = events
        self.min_time = events[0].time
        self.max_time = events[-1].time

        # Cache these variables so we don't need to compute
        self.has_ed_admission = self._has_ed_admission()
        self.has_admission = self._has_admission()
        self.has_discharge = self._has_discharge()

        # Infer the visit_type from the events
        # Admission takes precedence over ED
        if self.has_admission:
            self.visit_type = DEFAULT_INPATIENT_CONCEPT_ID
        elif self.has_ed_admission:
            self.visit_type = DEFAULT_ED_CONCEPT_ID
        else:
            self.visit_type = DEFAULT_OUTPATIENT_CONCEPT_ID

    def _has_ed_admission(self) -> bool:
        """
        Make this configurable in the future
        """
        for event in self.events:
            if 'ED_REGISTRATION' in event.code:
                return True
            if 'TRANSFER_TO//ED' in event.code:
                return True
        return False

    def _has_admission(self) -> bool:
        for event in self.events:
            if 'HOSPITAL_ADMISSION' in event.code:
                return True
        return False

    def _has_discharge(self) -> bool:
        for event in self.events:
            if 'HOSPITAL_DISCHARGE' in event.code:
                return True
        return False

    def get_discharge_facility(self) -> Optional[str]:
        if self._has_discharge():
            for event in self.events:
                if 'HOSPITAL_DISCHARGE' in event.code:
                    discharge_facility = event.code.replace('HOSPITAL_DISCHARGE', '')
                    discharge_facility = re.sub(r'[^a-zA-Z]', '', discharge_facility)
                    return discharge_facility
        return None

    def get_meds_events(self) -> List[Event]:
        return [
            Event(
                code=e.code.replace(' ', '_'),
                time=e.time,
                numeric_value=getattr(e, "numeric_value", None),
                text_value=getattr(e, "text_value", None),
                properties={'visit_id': self.visit_id, "table": "meds"}
            )
            for e in self.events
        ]


def convert_one_patient(
        patient: meds_reader.Patient,
        default_visit_id: int = 1,
        prediction_time: datetime = None,
        label: Union[int, float] = None
) -> CehrBertPatient:
    birth_datetime = None
    race = None
    gender = None
    ethnicity = None

    visit_id = default_visit_id
    current_date = None
    events_for_current_date = []
    patient_blocks = []
    for e in patient.events:

        # Skip out of the loop if the events's time stamps are beyond the prediction time
        if prediction_time is not None and e.time is not None:
            if e.time > prediction_time:
                break

        # This indicates demographics features
        if e.code in birth_codes:
            birth_datetime = e.time
        elif e.code.startswith('RACE'):
            race = e.code
        elif e.code.startswith('GENDER'):
            gender = e.code
        elif e.code.startswith('ETHNICITY'):
            ethnicity = e.code
        elif e.time is not None:
            if not current_date:
                current_date = e.time

            if current_date.date() == e.time.date():
                events_for_current_date.append(e)
            else:
                patient_blocks.append(PatientBlock(events_for_current_date, visit_id))
                events_for_current_date = list()
                events_for_current_date.append(e)
                current_date = e.time
                visit_id += 1

    if events_for_current_date:
        patient_blocks.append(PatientBlock(events_for_current_date, visit_id))

    admit_discharge_pairs = []
    active_ed_index = None
    active_admission_index = None
    # |ED|24-hours|Admission| ... |Discharge| -> ED will be merged into the admission (within 24 hours)
    # |ED|25-hours|Admission| ... |Discharge| -> ED will NOT be merged into the admission
    # |Admission|ED| ... |Discharge| -> ED will be merged into the admission
    # |Admission|Admission|ED| ... |Discharge|
    #   -> The first admission will be ignored and turned into a separate visit
    #   -> The second Admission and ED will be merged
    for i, patient_block in enumerate(patient_blocks):
        # Keep track of the ED block when there is no on-going admission
        if patient_block.has_ed_admission and active_admission_index is None:
            active_ed_index = i
        # Keep track of the admission block
        if patient_block.has_admission:
            # If the ED event has occurred, we need to check the time difference between
            # the ED event and the subsequent hospital admission
            if active_ed_index is not None:
                time_diff = relativedelta(
                    patient_blocks[active_ed_index].max_time,
                    patient_block.min_time
                )
                # If the time difference between the ed and admission is leq 24 hours,
                # we consider ED to be part of the visits
                if time_diff.hours <= 24 or active_ed_index == i:
                    active_admission_index = active_ed_index
                    active_ed_index = None
            else:
                active_admission_index = i

        if patient_block.has_discharge:
            if active_admission_index is not None:
                admit_discharge_pairs.append((active_admission_index, i))
            # When the patient is discharged from the hospital, we assume the admission and ED should end
            active_admission_index = None
            active_ed_index = None

        # Check the last block of the patient history to see whether the admission is partial
        if i == len(patient_blocks) - 1:
            # This indicates an ongoing (incomplete) inpatient visit,
            # this is a common pattern for inpatient visit prediction problems,
            # where the data from the first 24-48 hours after the admission
            # are used to predict something about the admission
            if active_admission_index is not None and prediction_time is not None:
                admit_discharge_pairs.append((active_admission_index, i))

    # Update visit_id for the admission blocks
    for admit_index, discharge_index in admit_discharge_pairs:
        admission_block = patient_blocks[admit_index]
        discharge_block = patient_blocks[discharge_index]
        visit_id = admission_block.visit_id
        for i in range(admit_index, discharge_index + 1):
            patient_blocks[i].visit_id = visit_id
            patient_blocks[i].visit_type = DEFAULT_INPATIENT_CONCEPT_ID
        # There could be events that occur after the discharge, which are considered as part of the visit
        # we need to check if the time stamp of the next block is within 12 hours
        if discharge_index + 1 < len(patient_blocks):
            next_block = patient_blocks[discharge_index + 1]
            hour_diff = (discharge_block.max_time - next_block.min_time).total_seconds() / 3600
            if hour_diff <= 12:
                next_block.visit_id = visit_id
                next_block.visit_type = DEFAULT_INPATIENT_CONCEPT_ID

    patient_block_dict = collections.defaultdict(list)
    for patient_block in patient_blocks:
        patient_block_dict[patient_block.visit_id].append(patient_block)

    visits = list()
    for visit_id, blocks in patient_block_dict.items():
        visit_type = blocks[0].visit_type
        visit_start_datetime = min([b.min_time for b in blocks])
        visit_end_datetime = max([b.max_time for b in blocks])
        discharge_facility = next(
            filter(None, [b.get_discharge_facility() for b in blocks]),
            None
        ) if visit_type == DEFAULT_INPATIENT_CONCEPT_ID else None
        visit_events = list()
        for block in blocks:
            visit_events.extend(block.get_meds_events())

        visits.append(
            Visit(
                visit_type=visit_type,
                visit_start_datetime=visit_start_datetime,
                visit_end_datetime=visit_end_datetime,
                discharge_facility=discharge_facility if discharge_facility else UNKNOWN_VALUE,
                events=visit_events
            )
        )
    age_at_index = -1
    if prediction_time is not None and birth_datetime is not None:
        age_at_index = prediction_time.year - birth_datetime.year
        if (prediction_time.month, prediction_time.day) < (birth_datetime.month, birth_datetime.day):
            age_at_index -= 1

    # birth_datetime can not be None
    assert birth_datetime is not None, f"patient_id: {patient.patient_id} does not have a valid birth_datetime"

    return CehrBertPatient(
        patient_id=patient.patient_id,
        birth_datetime=birth_datetime,
        visits=visits,
        race=race if race else UNKNOWN_VALUE,
        gender=gender if gender else UNKNOWN_VALUE,
        ethnicity=ethnicity if ethnicity else UNKNOWN_VALUE,
        index_date=prediction_time,
        age_at_index=age_at_index,
        label=label
    )


def create_dataset_from_meds_reader(
        data_args: DataTrainingArguments,
        default_visit_id: int = 1
) -> DatasetDict:
    train_dataset = _create_cehrbert_data_from_meds(
        data_args=data_args,
        split="train",
        default_visit_id=default_visit_id
    )
    tuning_dataset = _create_cehrbert_data_from_meds(
        data_args=data_args,
        split="tuning",
        default_visit_id=default_visit_id
    )
    held_out_dataset = _create_cehrbert_data_from_meds(
        data_args=data_args,
        split="held_out",
        default_visit_id=default_visit_id
    )

    return DatasetDict({
        "train": train_dataset,
        "validation": tuning_dataset,
        "test": held_out_dataset
    })


def _meds_to_cehrbert_generator(
        shards: List[Tuple[np.ndarray, np.ndarray, np.ndarray]],
        path_to_db: str,
        default_visit_id: int
) -> CehrBertPatient:
    for shard in shards:
        with meds_reader.PatientDatabase(path_to_db) as patient_database:
            for patient_id, prediction_time, label in shard:
                patient = patient_database[patient_id]
                yield convert_one_patient(patient, default_visit_id, prediction_time, label)


def _create_cehrbert_data_from_meds(
        data_args: DataTrainingArguments,
        split: str,
        default_visit_id: int = 1
):
    assert split in ['held_out', 'train', 'tuning']
    batches = []
    if data_args.cohort_folder:
        cohort = pd.read_parquet(os.path.join(data_args.cohort_folder, split))
        for cohort_row in cohort.itertuples():
            patient_id = cohort_row.patient_id
            prediction_time = cohort_row.prediction_time
            label = int(cohort_row.boolean_value)
            batches.append((patient_id, prediction_time, label))
    else:
        patient_split = get_patient_split(data_args.data_folder)
        for patient_id in patient_split[split]:
            batches.append((patient_id, None, None))

    split_batches = np.array_split(
        np.asarray(batches),
        data_args.preprocessing_num_workers
    )

    batch_func = functools.partial(
        _meds_to_cehrbert_generator,
        path_to_db=data_args.data_folder,
        default_visit_id=default_visit_id
    )
    dataset = Dataset.from_generator(
        batch_func,
        gen_kwargs={
            "shards": split_batches,
        },
        num_proc=data_args.preprocessing_num_workers,
        writer_batch_size=8
    )
    return dataset
