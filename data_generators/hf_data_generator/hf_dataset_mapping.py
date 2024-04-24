import datetime
from enum import Enum
from abc import abstractmethod, ABC
from typing import Dict, List, Any
import collections
import copy
from dateutil.relativedelta import relativedelta

from meds.schema import Patient, Event, birth_code
from med_extension.schema_extension import get_measurements_from_visit, Visit, CehrBertPatient
from spark_apps.decorators.patient_event_decorator import get_att_function
from models.hf_models.tokenization_hf_cehrbert import CehrBertTokenizer
from runner.hf_runner_argument_dataclass import DataTrainingArguments

# OMOP concept ids for inpatient related visits
INPATIENT_VISIT_TYPES = [
    '9201', '262', '8971', '8920', '38004311'
]
INPATIENT_VISIT_TYPE_CODES = [
    'Visit/IP', 'Visit/ERIP', 'Visit/51', 'Visit/61', 'NUCC/315D00000X'
]
DISCHARGE_FACILITY_TYPES = [
    '8536', '8863', '44814650', '4161979', '38004519', '4216643', '8717', '8920', '4021968',
    '8546', '8971', '8970', '44814649', '8827', '8676', '38003619', '8870', '4146681'
]

DATE_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class TruncationType(Enum):
    RANDOM_COMPLETE = "random_complete"
    RANDOM_RIGHT_TRUNCATION = "random_right_truncation"
    RANDOM_TRUNCATION = "random_truncation"
    TAIL = 'tail'


class DatasetMapping(ABC):

    def batch_transform(
            self,
            records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        return [self.transform(_) for _ in records]

    @abstractmethod
    def transform(
            self,
            record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Transform the record
        Args
            record: The row to process, as generated by the CDM processing
        Returns
            A dictionary from names to numpy arrays to be used by pytorch.
        """
        pass


def meds_to_cehrbert_extension(meds_record: Patient) -> CehrBertPatient:
    """
    Convert the MEDS Patient to the CerBertPatient extension, where the patient timeline is organized around visits
    """
    patient_id = meds_record['patient_id']
    static_measurements = meds_record['static_measurements']
    events = meds_record['events']

    assert len(events) >= 1

    birth_datetime = None
    race = None
    gender = None
    ethnicity = None

    visit_mapping = dict()
    # Iterate through all measurements
    for m, time in [(m, e['time']) for e in events for m in e['measurements']]:
        code = m['code']

        # Retrieve the demographics, the reason we are doing this is that the demographics
        # may not be stored in the first event.
        if code == birth_code:
            birth_datetime = time
        elif code.startswith('Race/'):
            race = code
        elif code.startswith('Gender/'):
            gender = code
        elif code.startswith('Ethnicity/'):
            ethnicity = code

        if m['metadata']['table'] == 'visit':
            is_inpatient = code in INPATIENT_VISIT_TYPE_CODES or code in INPATIENT_VISIT_TYPES

            visit_end_datetime = m['metadata']['end']
            if isinstance(visit_end_datetime, str):
                visit_end_datetime = datetime.datetime.strptime(visit_end_datetime, DATE_FORMAT)

            discharge_facility = m['metadata']['discharge_facility'] if is_inpatient else None
            visit_mapping[m['metadata']['visit_id']] = Visit(
                visit_type=code,
                visit_start_datetime=time,
                visit_end_datetime=visit_end_datetime,
                discharge_facility=discharge_facility,
                events=[]
            )

    # Add the events/measurements to the corresponding visit
    for e in events:
        for m in e['measurements']:
            # Remove the measurements without a visit_id, maybe these measurements should be connected to
            # the same visit_id since they have the same timestamp?
            if m['metadata']['visit_id'] and m['metadata']['table'] != 'visit':
                visit_mapping[m['metadata']['visit_id']]['events'].append(Event(time=e['time'], measurements=[m]))

    # Sort the events by timestamps
    for v in visit_mapping.values():
        v['events'] = sorted(v['events'], key=lambda e: e['time'])

    return CehrBertPatient(
        patient_id=patient_id,
        static_measurements=static_measurements,
        birth_datetime=birth_datetime,
        visits=list(visit_mapping.values()),
        race=race,
        gender=gender,
        ethnicity=ethnicity
    )


class MedToCehrBertDatasetMapping(DatasetMapping):
    def __init__(
            self,
            data_args: DataTrainingArguments
    ):
        self._time_token_function = get_att_function(data_args.att_function_type)
        self._include_auxiliary_token = data_args.include_auxiliary_token
        self._inpatient_time_token_function = get_att_function(data_args.inpatient_att_function_type)
        self._include_demographic_prompt = data_args.include_demographic_prompt

    """
    This mapping function converts the MED (https://github.com/Medical-Event-Data-Standard/meds/tree/main) extension
    to the CehrBert format. We make several assumptions
    - The first event contains the demographic information
    - From the second event onward
        - the time of the event is visit_start_datetime.
        - the first measurement contains the code indicating a standard OMOP Visit concept_id (e.g. 9201, 9202)
        - in case of inpatient visits, the last measurement is assumed to
            contain the standard OMOP concept id for discharge facilities (e.g 8536)
        - in case of inpatient visits, datetime_value of the last measurement stores visit_end_datetime
    """

    @staticmethod
    def _update_cehrbert_record(
            cehrbert_record: Dict[str, Any],
            code: str,
            visit_segment: int = 0,
            date: int = 0,
            age: int = -1,
            visit_concept_order: int = 0,
            visit_concept_id: str = '0',
            concept_value_mask: int = 0,
            concept_value: float = -1.,
            mlm_skip_value: int = 0,
    ) -> None:
        cehrbert_record['concept_ids'].append(code)
        cehrbert_record['visit_concept_orders'].append(visit_concept_order)
        cehrbert_record['ages'].append(age)
        cehrbert_record['dates'].append(date)
        cehrbert_record['visit_segments'].append(visit_segment)
        cehrbert_record['visit_concept_ids'].append(visit_concept_id)
        cehrbert_record['concept_value_masks'].append(concept_value_mask)
        cehrbert_record['concept_values'].append(concept_value)
        cehrbert_record['mlm_skip_values'].append(mlm_skip_value)

    def transform(
            self,
            med_record: Dict[str, Any]
    ) -> Dict[str, Any]:

        record = meds_to_cehrbert_extension(med_record)

        cehrbert_record = {
            'person_id': record['patient_id'],
            'concept_ids': [],
            'visit_segments': [],
            'orders': [],
            'dates': [],
            'ages': [],
            'visit_concept_orders': [],
            'concept_value_masks': [],
            'concept_values': [],
            'mlm_skip_values': [],
            'visit_concept_ids': []
        }
        # At least one visit should exist
        assert len(record['visits']) >= 1

        # Extract the demographic information
        birth_datetime = record['birth_datetime']
        gender = record['gender']
        race = record['race']

        if self._include_demographic_prompt:
            first_visit = record['visits'][0]
            year_str = f'year:{str(first_visit["visit_start_datetime"].year)}'
            age_str = f'age:{str(relativedelta(first_visit["visit_start_datetime"], birth_datetime).years)}'

            self._update_cehrbert_record(cehrbert_record, year_str)
            self._update_cehrbert_record(cehrbert_record, age_str)
            self._update_cehrbert_record(cehrbert_record, gender)
            self._update_cehrbert_record(cehrbert_record, race)

        # A bool indicator to toggle between 1 and 2
        visit_segment_indicator = False

        # Use a data cursor to keep track of time
        date_cursor = None

        # Loop through all the visits excluding the first event containing the demographics
        for i, visit in enumerate(sorted(record['visits'], key=lambda e: e['visit_start_datetime'])):

            measurements = get_measurements_from_visit(visit)

            # Skip this visit if the number measurements in the event is zero
            if not measurements:
                continue

            visit_start_datetime = visit['visit_start_datetime']
            time_delta = (visit_start_datetime - date_cursor).days if date_cursor else None
            date_cursor = visit_start_datetime

            # We assume the first measurement to be the visit type of the current visit
            visit_type = visit['visit_type']
            is_inpatient = visit_type in INPATIENT_VISIT_TYPES or visit_type in INPATIENT_VISIT_TYPE_CODES

            # Add artificial time tokens to the patient timeline if timedelta exists
            if time_delta:
                # This generates an artificial time token depending on the choice of the time token functions
                self._update_cehrbert_record(
                    cehrbert_record,
                    code=self._time_token_function(time_delta),
                    visit_concept_order=i + 1
                )

            # Add the VS token to the patient timeline to mark the start of a visit
            age = relativedelta(visit['visit_start_datetime'], birth_datetime).years
            # Calculate the week number since the epoch time
            date = (visit['visit_start_datetime'] - datetime.datetime(year=1970, month=1, day=1)).days // 7
            visit_segment = int(visit_segment_indicator) + 1

            self._update_cehrbert_record(
                cehrbert_record,
                code='[VS]',
                visit_concept_order=i + 1,
                age=age,
                date=date,
                visit_segment=visit_segment,
                visit_concept_id=visit_type
            )

            if self._include_auxiliary_token:
                self._update_cehrbert_record(
                    cehrbert_record,
                    code=visit_type,
                    visit_concept_order=i + 1,
                    age=age,
                    date=date,
                    visit_segment=visit_segment,
                    visit_concept_id=visit_type
                )

            # Sort all measurements using time, in case of a tie, we use the natural order of codes to tiebreak
            for m_i, m in enumerate(sorted(measurements, key=lambda m: (m['datetime_value'], m['code']))):
                # Add a medical token to the patient timeline
                # If this is an inpatient visit, we use the event time stamps to calculate age and date
                # because the patient can stay in the hospital for a period of time.
                if is_inpatient:
                    # Calculate age using the event time stamp
                    age = relativedelta(m['datetime_value'], birth_datetime).years
                    # Calculate the week number since the epoch time
                    date = (m['datetime_value'] - datetime.datetime(year=1970, month=1, day=1)).days // 7
                else:
                    # For outpatient visits, we use the visit time stamp to calculate age and time because we assume
                    # the outpatient visits start and end on the same day
                    pass

                # Calculate the time diff in days w.r.t the previous measurement
                meas_time_diff = relativedelta(m['datetime_value'], date_cursor).days
                # Update the date_cursor if the time diff between two neighboring measurements is greater than and
                # equal to 1 day
                if meas_time_diff > 0:
                    date_cursor = m['datetime_value']
                    if self._inpatient_time_token_function:
                        # This generates an artificial time token depending on the choice of the time token functions
                        self._update_cehrbert_record(
                            cehrbert_record,
                            code=f'i-{self._inpatient_time_token_function(time_delta)}',
                            visit_concept_order=i + 1,
                            visit_segment=visit_segment,
                            visit_concept_id=visit_type
                        )

                # If numeric_value exists, this is a concept/value tuple, we indicate this using a concept_value_mask
                concept_value_mask = int('numeric_value' in m)
                concept_value = m['numeric_value'] if 'numeric_value' in m else -1

                self._update_cehrbert_record(
                    cehrbert_record,
                    code=m['code'],
                    age=age,
                    date=date,
                    visit_concept_order=i + 1,
                    visit_segment=visit_segment,
                    visit_concept_id=visit_type,
                    concept_value_mask=concept_value_mask,
                    concept_value=concept_value,
                    mlm_skip_value=int('numeric_value' in m)
                )

            if is_inpatient:
                # If visit_end_datetime is populated for the inpatient visit, we update the date_cursor
                visit_end_datetime = visit.get('visit_end_datetime', None)
                if visit_end_datetime:
                    date_cursor = visit_end_datetime

                if self._include_auxiliary_token:
                    # Reuse the age and date calculated for the last event in the patient timeline for the discharge
                    # facility event
                    discharge_facility = visit['discharge_facility'] if 'discharge_facility' in visit else '0'

                    self._update_cehrbert_record(
                        cehrbert_record,
                        code=discharge_facility,
                        age=age,
                        date=date,
                        visit_concept_order=i + 1,
                        visit_segment=visit_segment,
                        visit_concept_id=visit_type
                    )

            # Reuse the age and date calculated for the last event in the patient timeline
            self._update_cehrbert_record(
                cehrbert_record,
                code='[VE]',
                age=age,
                date=date,
                visit_concept_order=i + 1,
                visit_segment=visit_segment,
                visit_concept_id=visit_type
            )

            # Toggle visit_segment_indicator
            visit_segment_indicator = not visit_segment_indicator

        # Generate the orders of the concepts that the cehrbert dataset mapping function expects
        cehrbert_record['orders'] = list(range(1, len(cehrbert_record['concept_ids']) + 1))

        # Add some count information for this sequence
        cehrbert_record['num_of_concepts'] = len(cehrbert_record['concept_ids'])
        cehrbert_record['num_of_visits'] = len(record['visits'])

        # Add demographics for this patient
        cehrbert_record['birth_datetime'] = birth_datetime
        cehrbert_record['gender'] = gender
        cehrbert_record['race'] = race

        return cehrbert_record


class SortPatientSequenceMapping(DatasetMapping):
    """
    A mapping function to order all the features using a pre-defined orders/dates column.
    This may not be necessary since the order is feature columns should've been ordered
    correctly during the data generation process in the spark application. However,
    it's a good idea to sort them explicitly one more time
    """

    def transform(
            self,
            record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Sort all the list features using a pre-defined orders/dates. If orders/dates columns are not provided,
        do nothing.
        """

        sorting_columns = record.get('orders', None)
        if not sorting_columns:
            sorting_columns = record.get('dates', None)

        if not sorting_columns:
            return record

        sorting_columns = list(map(int, sorting_columns))
        seq_length = len(record['concept_ids'])
        column_names = ['concept_ids']
        column_values = [record['concept_ids']]

        for k, v in record.items():
            if k in column_names:
                continue
            if isinstance(v, list) and len(v) == seq_length:
                column_names.append(k)
                column_values.append(v)

        sorted_list = sorted(zip(sorting_columns, *column_values), key=lambda tup2: (tup2[0], tup2[1]))

        # uses a combination of zip() and unpacking (*) to transpose the list of tuples. This means converting rows
        # into columns: the first tuple formed from all the first elements of the sorted tuples, the second tuple
        # from all the second elements, and so on. Then slices the resulting list of tuples to skip the first tuple
        # (which contains the sorting criteria) and retain only the data columns.
        sorted_features = list(zip(*list(sorted_list)))[1:]
        new_record = collections.OrderedDict()
        for i, new_val in enumerate(sorted_features):
            new_record[column_names[i]] = list(new_val)
        return new_record


class HFTokenizationMapping(DatasetMapping):
    def __init__(
            self,
            concept_tokenizer: CehrBertTokenizer,
            is_pretraining: bool
    ):
        self._concept_tokenizer = concept_tokenizer
        self._is_pretraining = is_pretraining

    def transform(
            self,
            record: Dict[str, Any]
    ) -> Dict[str, Any]:

        # if 'start_index' not in record:
        #     raise ValueError('Missing start_index in row')
        #
        # if 'end_index' not in record:
        #     raise ValueError('Missing end_index in row')
        #
        # start_index = record['start_index']
        # end_index = record['end_index']
        #
        # seq_length = len(record['concept_ids'])
        # new_record = collections.OrderedDict()
        # for k, v in record.items():
        #     if isinstance(v, list) and len(v) == seq_length:
        #         new_record[k] = v[start_index:end_index]
        #
        # assert max(new_record['visit_concept_orders']) - min(new_record['visit_concept_orders']) < 512, \
        #     (f"start_index: {start_index}, end_index: {end_index}, person_id: {new_record['person_id']}\n"
        #      f"max visit_concept_order: {max(new_record['visit_concept_orders'])}\n"
        #      f"min visit_concept_order: {min(new_record['visit_concept_orders'])}\n"
        #      f"visit_concept_order: {new_record['visit_concept_orders']}")

        input_ids = self._concept_tokenizer.encode(record['concept_ids'])
        record['input_ids'] = input_ids

        # If mlm_skip_value=1, this indicates there is a value associated with this position and
        # hence we block the MLM to randomly pick this token to be predicted
        if self._is_pretraining:
            if 'mlm_skip_values' in record:
                labels = copy.deepcopy(input_ids)
                mlm_skip_values = record['mlm_skip_values']
                if len(input_ids) != len(mlm_skip_values):
                    self._concept_tokenizer.encode(record['concept_ids'])

                assert len(input_ids) == len(mlm_skip_values), \
                    f"The following equality must be true: len(input_ids) == len(mlm_skip_values)"

                for i, (input_id, mlm_skip_value) in enumerate(zip(input_ids, mlm_skip_values)):
                    if mlm_skip_value == 1:
                        labels[i] = -100

                record.update({
                    'input_ids': input_ids,
                    'labels': labels
                })

        return record


class HFFineTuningMapping(DatasetMapping):
    def transform(
            self,
            record: Dict[str, Any]
    ) -> Dict[str, Any]:
        # if 'start_index' not in record:
        #     raise ValueError('Missing start_index in row')
        #
        # if 'end_index' not in record:
        #     raise ValueError('Missing end_index in row')

        new_record = copy.deepcopy(record)
        new_record.update({
            'age_at_index': record['age'],
            'classifier_label': record['label']
        })
        return new_record
