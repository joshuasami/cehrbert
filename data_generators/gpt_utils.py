import re
import random
from datetime import date, timedelta
from typing import Sequence

inpatient_att_pattern = re.compile(r'(?:VS-|i-)D(\d+)(?:-VE)?')


class RandomSampleCache:
    def __init__(self, data_indices: Sequence[int], cache_size: int, sample_weights: Sequence[float] = None):
        self._data_indices = data_indices
        self._sample_weights = sample_weights
        self._cache_size = cache_size
        self._cache = []

        if self._sample_weights is not None:
            assert sum(self._sample_weights) - 1 < 1e-8

    def next(self):
        if not self._cache:
            if self._sample_weights is not None:
                self._cache.extend(
                    random.choices(self._data_indices, k=self._cache_size, weights=self._sample_weights)
                )
            else:
                self._cache.extend(
                    random.choices(self._data_indices, k=self._cache_size)
                )
        return self._cache.pop()


def random_slice_gpt_sequence(
        concept_ids,
        max_seq_len
):
    seq_length = len(concept_ids)
    starting_points = []
    [start_year, start_age, start_gender, start_race] = [_ for _ in concept_ids[0:4]]
    try:
        start_year = int(start_year.split(':')[1])
        start_age = int(start_age.split(':')[1])
        data_cursor = date(int(start_year), 1, 1)
        birth_date = date(start_year - start_age, 1, 1)
        for i in range(4, max(5, seq_length - max_seq_len)):
            current_token = concept_ids[i]
            if current_token == 'VS':
                starting_points.append((i, data_cursor.year, data_cursor.year - birth_date.year))
            elif current_token[0] == 'D':
                att_date_delta = int(current_token[1:])
                data_cursor = data_cursor + timedelta(days=att_date_delta)
            elif current_token == 'LT':
                att_date_delta = 365 * 3
                data_cursor = data_cursor + timedelta(days=att_date_delta)
            elif current_token[:3] == 'VS-':  # VS-D7-VE
                data_cursor = data_cursor + timedelta(days=int(current_token.split('-')[1][1:]))
            elif current_token[:2] == 'i-':  # i-D7
                data_cursor = data_cursor + timedelta(days=int(current_token.split('-')[1][1:]))

        if len(starting_points) == 0:
            return 0, 0, concept_ids[0:4]

        random_starting_index, random_starting_year, random_starting_age = random.choice(starting_points)
        demographic_tokens = [
            f'year:{random_starting_year}',
            f'age:{random_starting_age}',
            start_gender,
            start_race
        ]
        # Remove the number of demographic tokens
        random_end_index = random_starting_index
        for i in reversed(list(range(random_starting_index, random_starting_index + max_seq_len - 4))):
            current_token = concept_ids[i]
            if current_token == 'VE':
                random_end_index = i
                break
        # new_token_ids = demographic_tokens + concept_ids[random_starting_index:random_end_index + 1]
        return random_starting_index, random_end_index, demographic_tokens
    except Exception as e:
        return 0, max_seq_len - 1, []
