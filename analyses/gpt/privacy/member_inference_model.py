import logging

import os
import sys
import pandas as pd
from tqdm import tqdm
from datetime import datetime
import tensorflow as tf

from utils.model_utils import tokenize_one_field
from models.model_parameters import ModelPathConfig
from data_generators.data_generator_base import GptDataGenerator
from data_generators.learning_objective import CustomLearningObjective
from models.layers.custom_layers import get_custom_objects

logger = logging.getLogger('member_inference_model')


def main(args):
    config = ModelPathConfig(args.attack_data_folder, args.model_folder)
    attack_data = pd.read_parquet(args.attack_data_folder)
    tokenizer = tokenize_one_field(
        attack_data,
        'concept_ids',
        'token_ids',
        config.tokenizer_path
    )

    gpt_data_generator = GptDataGenerator(
        training_data=attack_data,
        batch_size=args.batch_size,
        max_seq_len=512,
        concept_tokenizer=tokenizer,
        min_num_of_visits=2,
        max_num_of_visits=1e10,
        min_num_of_concepts=20,
        including_long_sequence=False,
        sampling_dataset_enabled=False,
        is_random_cursor=False,
        is_pretraining=False
    )
    gpt_data_generator._learning_objectives.append(
        CustomLearningObjective(input_schema={'person_id': tf.int32}, output_schema={'label': tf.int32})
    )

    strategy = tf.distribute.MirroredStrategy()
    logger.info('Number of devices: {}'.format(strategy.num_replicas_in_sync))
    with strategy.scope():
        existing_model_path = os.path.join(args.model_folder, 'bert_model.h5')
        if not os.path.exists(existing_model_path):
            sys.exit(f'The model can not be loaded from {existing_model_path}!')
        logger.info(f'The model is loaded from {existing_model_path}')
        model = tf.keras.models.load_model(existing_model_path, custom_objects=get_custom_objects())

    person_ids = []
    losses = []
    labels = []
    iterator = gpt_data_generator.create_batch_generator()

    for each_batch in tqdm(iterator, total=gpt_data_generator.get_steps_per_epoch()):

        inputs, outputs = each_batch
        person_ids.extend(inputs['person_id'])
        labels.extend(outputs['label'])
        predictions = model.predict(inputs['concept_ids'])
        y_true_val = outputs['concept_predictions'][:, :, 0]
        mask = tf.cast(outputs['concept_predictions'][:, :, 1], dtype=tf.float32)
        loss = tf.keras.losses.sparse_categorical_crossentropy(y_true_val, predictions)
        masked_loss = tf.reduce_mean(loss * mask, axis=-1).numpy()
        losses.extend(tf.reduce_mean(masked_loss, axis=-1).numpy().tolist())

        if len(labels) > 0 and len(labels) % args.buffer_size == 0:
            results_df = pd.DataFrame(zip(person_ids, losses, labels), columns=['person_id', 'loss', 'label'])
            current_time = datetime.now().strftime("%m-%d-%Y-%H-%M-%S")
            results_df.to_parquet(os.path.join(args.output_folder, f'{current_time}.parquet'))
            # Clear the lists for the next batch
            person_ids.clear()
            losses.clear()
            labels.clear()

    if len(labels) > 0:
        results_df = pd.DataFrame(zip(person_ids, losses, labels), columns=['person_id', 'loss', 'label'])
        current_time = datetime.now().strftime("%m-%d-%Y-%H-%M-%S")
        results_df.to_parquet(os.path.join(args.output_folder, f'{current_time}.parquet'))


def create_argparser():
    import argparse
    parser = argparse.ArgumentParser(
        description='Membership Inference Analysis Arguments using the GPT model'
    )
    parser.add_argument(
        '--attack_data_folder',
        dest='attack_data_folder',
        action='store',
        help='The path for where the attack data folder',
        required=True
    )
    parser.add_argument(
        '--output_folder',
        dest='output_folder',
        action='store',
        help='The output folder that stores the metrics',
        required=True
    )
    parser.add_argument(
        '--model_folder',
        dest='model_folder',
        action='store',
        help='The path to trained model folder',
        required=True
    )
    parser.add_argument(
        '--batch_size',
        dest='batch_size',
        type=int,
        action='store',
        required=False,
        default=256
    )
    parser.add_argument(
        '--buffer_size',
        dest='buffer_size',
        type=int,
        action='store',
        required=False,
        default=1024
    )
    return parser


if __name__ == "__main__":
    gpus = tf.config.list_physical_devices('GPU')
    if gpus:
        try:
            # Currently, memory growth needs to be the same across GPUs
            for gpu in gpus:
                tf.config.experimental.set_memory_growth(gpu, True)
            logical_gpus = tf.config.list_logical_devices('GPU')
            print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
        except RuntimeError as e:
            # Memory growth must be set before GPUs have been initialized
            print(e)
    main(create_argparser().parse_args())
