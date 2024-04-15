import logging
from enum import Enum
from abc import abstractmethod, ABC
from typing import Dict, Any
import collections
import random
import numpy as np
import copy
from models.hf_models.tokenization_hf_cehrbert import CehrBertTokenizer

logger = logging.getLogger(__name__)


class TruncationType(Enum):
    RANDOM = 'random'
    TAIL = 'tail'


class DatasetMapping(ABC):

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


class GenerateStartEndIndexMapping(DatasetMapping):
    def __init__(
            self,
            max_sequence_length,
            truncate_type=TruncationType.RANDOM
    ):
        self._max_sequence_length = max_sequence_length
        self._truncate_type = truncate_type

    def transform(
            self,
            record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Adapted from https://github.com/OHDSI/Apollo/blob/main/data_loading/data_transformer.py

        Adding the start and end indices to extract a portion of the patient sequence
        """

        seq_length = len(record['concept_ids'])
        new_max_length = self._max_sequence_length - 1  # Subtract one for the [CLS] token
        if seq_length > new_max_length and self._truncate_type == TruncationType.RANDOM:
            start_index = random.randint(0, seq_length - new_max_length)
            end_index = min(seq_length, start_index + new_max_length)
            record['start_index'] = start_index
            record['end_index'] = end_index
        else:
            record['start_index'] = max(0, seq_length - new_max_length)
            record['end_index'] = seq_length
        return record


class HFMaskedLanguageModellingMapping(DatasetMapping):
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

        if 'start_index' not in record:
            raise ValueError('Missing start_index in row')

        if 'end_index' not in record:
            raise ValueError('Missing end_index in row')

        start_index = record['start_index']
        end_index = record['end_index']

        seq_length = len(record['concept_ids'])
        new_record = collections.OrderedDict()
        for k, v in record.items():
            if isinstance(v, list) and len(v) == seq_length:
                new_record[k] = v[start_index:end_index]

        input_ids = self._concept_tokenizer.encode(new_record['concept_ids'])

        new_record.update({
            'input_ids': input_ids
        })

        if self._is_pretraining:
            masked_input_ids, output_mask = self._mask_concepts(input_ids, new_record['mlm_skip_values'])
            masks = np.empty_like(masked_input_ids, dtype=np.int32)
            # -100 is ignored by the torch CrossEntropyLoss
            masks.fill(-100)
            labels = np.where(output_mask == 1, input_ids, masks)
            new_record.update({
                'input_ids': masked_input_ids.tolist(),
                'labels': labels.tolist()
            })

        return new_record

    def _mask_concepts(self, concepts, mlm_skip_values):
        """
        Mask out 15% of the concepts

        :param concepts:
        :param mlm_skip_values:
        :return:
        """

        masked_concepts = np.asarray(concepts).copy()
        output_mask = np.zeros((len(concepts),), dtype=int)

        for word_pos in range(0, len(concepts)):
            # Check if this position needs to be skipped
            if mlm_skip_values[word_pos] == 1:
                continue
            if concepts[word_pos] == self._concept_tokenizer.unused_token_index:
                break
            if random.random() < 0.15:
                dice = random.random()
                if dice < 0.8:
                    masked_concepts[word_pos] = self._concept_tokenizer.mask_token_index
                elif dice < 0.9:
                    masked_concepts[word_pos] = random.randint(
                        0,
                        self._concept_tokenizer.vocab_size - 1
                    )
                # else: 10% of the time we just leave the word as is
                output_mask[word_pos] = 1

        return masked_concepts, output_mask


class HFFineTuningMapping(DatasetMapping):
    def transform(
            self,
            record: Dict[str, Any]
    ) -> Dict[str, Any]:
        if 'start_index' not in record:
            raise ValueError('Missing start_index in row')

        if 'end_index' not in record:
            raise ValueError('Missing end_index in row')

        new_record = copy.deepcopy(record)
        new_record.update({
            'age_at_index': record['age'],
            'classifier_label': record['label']
        })
        return new_record
