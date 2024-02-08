from abc import abstractmethod, ABC
import re

import numpy as np
import tensorflow as tf

from data_generators.tokenizer import ConceptTokenizer
from keras_transformer.extras import ReusableEmbedding, TiedOutputEmbedding
from models.layers.custom_layers import (
    GptDecoder, PositionalEncodingLayer, ConceptValueTransformationLayer, ConceptValuePredictionLayer
)

INPATIENT_ATT_PATTERN = r"VS-.\d+-VE"


class SamplingStrategy(ABC):
    @abstractmethod
    def process_logit(self, outputs):
        pass

    @abstractmethod
    def get_name(self):
        pass


class TopKStrategy(SamplingStrategy):
    def __init__(
            self,
            top_k,
            temperature: float = 1.0
    ):
        self._top_k = top_k
        self._temperature = temperature

    def process_logit(self, outputs):
        # Randomly sample a batch of tokens
        pred_logits, indices = tf.math.top_k(outputs[:, -1, :] / self._temperature, k=self._top_k, sorted=True)
        return pred_logits, indices

    def get_name(self):
        return 'top_k_strategy'


class TopPStrategy(SamplingStrategy):
    def __init__(
            self,
            top_p,
            temperature: float = 1.0
    ):
        self._top_p = top_p
        self._temperature = temperature

    def process_logit(self, outputs):
        # Top P strategy
        indices = tf.argsort(outputs[:, -1, :], direction='DESCENDING')
        probs = tf.nn.softmax(outputs[:, -1, :] / self._temperature)
        sorted_probs = tf.gather(probs, indices, axis=1, batch_dims=1)
        cum_sum_probs = tf.math.cumsum(sorted_probs, axis=-1)

        included_top_probs = tf.cast(cum_sum_probs >= self._top_p, dtype=tf.float32)
        prob_mask = tf.concat([tf.zeros_like(included_top_probs[:, :1]), included_top_probs[:, :-1]], axis=-1)
        pred_logit = tf.math.log(sorted_probs) + prob_mask * (-1e10)

        return pred_logit, indices

    def get_name(self):
        return 'top_p_strategy'


class GptInferenceModel(tf.keras.Model):
    def __init__(
            self,
            gpt_model: tf.keras.Model,
            tokenizer: ConceptTokenizer,
            context_window: int,
            sampling_strategy: SamplingStrategy,
            *args,
            **kwargs
    ):
        super(GptInferenceModel, self).__init__(*args, **kwargs)

        self.context_window = context_window
        self.tokenizer = tokenizer
        self.sampling_strategy = sampling_strategy
        self.concept_embedding_layer = self._get_concept_embedding(gpt_model)
        self.positional_encoding_layer = self._get_positional_encoding_layer(gpt_model)
        self.output_layer = self._get_output_layer(gpt_model)
        self.gpt_decoder = self._get_gpt_decoder(gpt_model)
        self.vocab_size = self.concept_embedding_layer.input_dim
        self.att_token_ids = self._get_att_token_ids()
        self.inpat_att_token_ids = self._get_inpatient_att_token_ids()
        self.span_separators = self._get_span_separators()
        self.all_att_token_ids = self.att_token_ids + self.inpat_att_token_ids

    def _get_att_token_ids(self):
        """
        We assume we create artificial tokens before 1080 days
        :return:
        """
        day_tokens = [f'D{i}' for i in range(1080)]
        month_tokens = [f'M{i}' for i in range(40)]
        quarter_tokens = [f'Q{i}' for i in range(12)]
        year_tokens = [f'Y{i}' for i in range(3)]
        other_tokens = ['LT']

        att_tokens = day_tokens + month_tokens + quarter_tokens + year_tokens + other_tokens

        att_token_ids = []
        for token, token_id in self.tokenizer.tokenizer.word_index.items():
            if token in att_tokens:
                att_token_ids.append(token_id)

        return att_token_ids

    def _get_inpatient_att_token_ids(self):
        separator_indexes = []
        # We collect all the inpatient att tokens
        for w, i in self.tokenizer.tokenizer.word_index.items():
            if re.match(INPATIENT_ATT_PATTERN, w):
                separator_indexes.append(i)
        return separator_indexes

    def _get_span_separators(self):
        separator_indexes = []
        for w, i in self.tokenizer.tokenizer.word_index.items():
            if 'VS' in w:
                separator_indexes.append(i)
        return separator_indexes

    def _generate_next_token(
            self,
            generated_tokens,
            visit_concept_orders,
            cached_contexts
    ):
        current_length = tf.shape(generated_tokens)[-1]
        previous_length = tf.shape(cached_contexts)[2] if cached_contexts is not None else 0

        first_layer_context, concept_embedding_matrix = self.concept_embedding_layer(
            generated_tokens,
            training=False
        )
        # Add the positional embeddings
        first_layer_context += self.positional_encoding_layer(
            visit_concept_orders
        )

        look_ahead_mask_base = tf.cast(
            1 - tf.linalg.band_part(
                tf.ones((current_length, current_length)), -1, 0
            ),
            dtype=tf.int32
        )[tf.newaxis, tf.newaxis, previous_length:current_length, :]

        if cached_contexts is not None:
            # Slice out the new token representations
            x = first_layer_context[:, previous_length: current_length]
        else:
            x = first_layer_context

        layer_contexts = []
        for i in range(self.gpt_decoder.num_layers):
            if i == 0:
                value = first_layer_context
            elif cached_contexts is not None:
                # Concat the previous context with the x representation
                value = tf.concat([cached_contexts[i - 1], x], axis=1)
            else:
                value = x

            x, attn_weights = self.gpt_decoder.decoder_layers[i](
                query=x,
                key=value,
                value=value,
                decoder_mask=look_ahead_mask_base,
                training=False
            )
            layer_contexts.append(x)

        layer_contexts = tf.stack(layer_contexts, axis=0)

        if cached_contexts is not None:
            new_cached_contexts = tf.concat([cached_contexts, layer_contexts], axis=2)
        else:
            new_cached_contexts = layer_contexts

        logtis = self.output_layer(
            [x, concept_embedding_matrix],
            training=False
        )

        return logtis, new_cached_contexts

    def _block_recurring_tokens(
            self,
            inputs,
            outputs,
            disallowed_token_ids
    ):
        batch, length = tf.shape(inputs)
        # Get the index of the last occurrence of VS token in the sequence
        last_vs_token_index = self._find_last_index_of_token(
            sequence_in_batch=inputs,
            token_ids=self.span_separators
        )

        # If this is a new visit, indicated by last_vs_token_index == length - 1, we will clear out the memory
        new_visit_indicator = tf.cast(last_vs_token_index == length - 1, dtype=tf.float32)
        disallowed_token_ids = (1 - new_visit_indicator) * disallowed_token_ids

        # Mask token ids by adding -1e9 to their corresponding logits
        masked_outputs = (disallowed_token_ids * -1e9)[:, tf.newaxis, :] + outputs
        return masked_outputs, disallowed_token_ids

    def call(
            self,
            inputs
    ):

        # Get the current sequence length
        batch, length = tf.shape(
            inputs
        )
        #  Create a cache contexts to store the previous states
        cached_contexts = None
        disallowed_token_ids = tf.zeros((
            batch,
            self.tokenizer.get_vocab_size()
        ))
        # Create the default visit_concept_orders for the demographic prompts
        # TODO: this may not be a good assumption cos the inputs could be partial history
        visit_concept_orders = tf.zeros_like(
            inputs
        )

        while length < self.context_window:
            # Generate the next batch of tokens and update the contexts
            outputs, cached_contexts = self._generate_next_token(
                inputs,
                visit_concept_orders,
                cached_contexts
            )
            # Block the sampling of the tokens that already appear within the same visit (defined
            # as the tokens that appear since the last VS)
            outputs, disallowed_token_ids = self._block_recurring_tokens(
                inputs,
                outputs,
                disallowed_token_ids
            )
            # Randomly sample a batch of tokens
            pred_logits, indices = self.sampling_strategy.process_logit(outputs)
            indices = np.asarray(indices).astype('int32')
            next_tokens = tf.gather(
                indices,
                tf.random.categorical(pred_logits, 1),
                axis=1,
                batch_dims=1
            ).numpy()

            disallowed_token_ids += tf.one_hot(
                tf.squeeze(next_tokens),
                self.tokenizer.get_vocab_size()
            )

            # Check if any of the current token is att tokens
            att_token_indicators = tf.cast(
                tf.reduce_any(inputs[:, -1:] == self.att_token_ids, axis=-1),
                dtype=tf.int32
            )[:, tf.newaxis]

            # Replace the next tokens with VS token if the current token is an ATT token
            next_tokens = (
                    att_token_indicators * self.tokenizer.get_visit_start_token_id() +
                    (1 - att_token_indicators) * next_tokens
            )

            # Stitch up the new tokens and previously generated tokens
            inputs = np.hstack(
                [inputs, next_tokens]
            )

            # if the previous visit_concept_orders = 0, this indicates a starting point of a sequence
            start_span_indicators = visit_concept_orders[:, -1:] == 0

            # For all ATT tokens including inter/intra ones, we set the corresponding next_visit_concept_orders to
            # the previous next_visit_concept_orders + 1
            all_att_token_indicators = tf.reduce_any(
                next_tokens[..., tf.newaxis] == self.all_att_token_ids,
                axis=-1
            )

            # Combine these two indicators via logical or operation.
            new_span_indicators = tf.cast(
                tf.logical_or(start_span_indicators, all_att_token_indicators),
                dtype=tf.int32
            )

            # If the previous visit_concept_orders equals 0, this indicates a new span therefore we copy the one
            # before the previous visit_concept_orders and increment by 1. When the previous visit_concept_orders
            # is NOT 0, we simply copy the previous visit_concept_orders
            next_visit_concept_orders = (
                    new_span_indicators * (visit_concept_orders[:, -2:-1] + 1)
                    + (1 - new_span_indicators) * visit_concept_orders[:, -1:]
            )

            visit_concept_orders = np.hstack([
                visit_concept_orders,
                next_visit_concept_orders
            ])

            # Get the new length of the sequence
            _, length = np.shape(
                inputs
            )

            # This indicates all the sequences have ended
            if np.all(np.any(inputs == self.tokenizer.get_end_token_id(), axis=-1)):
                break

        return inputs

    @staticmethod
    def _find_last_index_of_token(
            sequence_in_batch,
            token_ids
    ):
        length = tf.shape(sequence_in_batch)[1].numpy()
        first_token_index = tf.reduce_any(sequence_in_batch[:, :, tf.newaxis] == token_ids, axis=-1)
        argmax_in_reverse = tf.argmax(tf.reverse(first_token_index, [-1]), axis=-1)
        last_token_index = length - argmax_in_reverse[:, tf.newaxis] - 1
        return tf.cast(last_token_index, dtype=tf.int32)

    @staticmethod
    def _get_concept_embedding(gpt_model):
        layers = [layer for layer in gpt_model.layers if isinstance(layer, ReusableEmbedding)]
        if len(layers) == 0:
            raise RuntimeError(f'Could not find ReusableEmbedding')
        return layers[0]

    @staticmethod
    def _get_output_layer(gpt_model):
        layers = [layer for layer in gpt_model.layers if isinstance(layer, TiedOutputEmbedding)]
        if len(layers) == 0:
            raise RuntimeError(f'Could not find TiedOutputEmbedding')
        return layers[0]

    @staticmethod
    def _get_positional_encoding_layer(gpt_model):
        gpt_model.get_layer('positional_encoding_layer')
        layers = [layer for layer in gpt_model.layers if
                  isinstance(layer, PositionalEncodingLayer)]
        if len(layers) == 0:
            raise RuntimeError(f'Could not find GptPositionEmbedding')
        return layers[0]

    @staticmethod
    def _get_gpt_decoder(gpt_model):
        layers = [layer for layer in gpt_model.layers if isinstance(layer, GptDecoder)]
        if len(layers) == 0:
            raise RuntimeError(f'Could not find GPT Decoder')
        return layers[0]


def create_model(
        context_window_size,
        vocab_size,
        embedding_size,
        num_heads,
        depth,
        embedding_dropout: float = 0.6,
        include_numeric_value: bool = False
):
    """
    model = create_model(
        max_len=100,
        vocab_size=100,
        embed_dim=128,
        num_heads=16,
        num_of_layers=5
    )
    :param context_window_size:
    :param vocab_size:
    :param embedding_size:
    :param num_heads:
    :param depth:
    :param embedding_dropout:
    :param include_numeric_value
    :return:
    """
    concept_inputs = tf.keras.layers.Input(
        shape=(None,),
        dtype=tf.int32,
        name='concept_ids'
    )

    visit_concept_orders = tf.keras.layers.Input(
        shape=(None,),
        dtype=tf.int32,
        name='visit_concept_orders'
    )

    model_inputs = [concept_inputs, visit_concept_orders]

    concept_embedding_layer = ReusableEmbedding(
        vocab_size, embedding_size,
        name='concept_embeddings',
        # Regularization is based on paper "A Comparative Study on
        # Regularization Strategies for Embedding-based Neural Networks"
        # https://arxiv.org/pdf/1508.03721.pdf
        embeddings_regularizer=tf.keras.regularizers.l2(1e-4)
    )

    positional_encoding_layer = PositionalEncodingLayer(
        max_sequence_length=context_window_size,
        embedding_size=embedding_size,
        name='positional_encoding_layer'
    )

    output_layer = TiedOutputEmbedding(
        projection_regularizer=tf.keras.regularizers.l2(1e-4),
        projection_dropout=embedding_dropout,
        name='concept_prediction_logits'
    )

    # embeddings for encoder input
    original_concept_embeddings, concept_embedding_matrix = concept_embedding_layer(concept_inputs)

    x = original_concept_embeddings + positional_encoding_layer(
        visit_concept_orders
    )

    # If this flag is enabled, we will include additional inputs to incorporate the numeric values into the model
    if include_numeric_value:
        concept_values = tf.keras.layers.Input(
            shape=(None,),
            dtype=tf.float32,
            name='concept_values'
        )

        concept_value_masks = tf.keras.layers.Input(
            shape=(None,),
            dtype=tf.int32,
            name='concept_value_masks'
        )

        value_transformation_layer = ConceptValueTransformationLayer(
            embedding_size=embedding_size,
            name='value_transformation_layer'
        )

        x = value_transformation_layer(
            x, concept_values, concept_value_masks
        )

        model_inputs.extend([concept_values, concept_value_masks])

    transformer_block = GptDecoder(
        depth,
        embedding_size,
        num_heads,
        name='decoder'
    )

    contextualized_embeddings, _, _ = transformer_block(
        x
    )

    concept_prediction_layer = tf.keras.layers.Softmax(
        name='concept_predictions'
    )

    concept_predictions = concept_prediction_layer(
        output_layer([contextualized_embeddings, concept_embedding_matrix])
    )
    model_outputs = [concept_predictions]

    # If this flag is enabled, we will include an additional learning objective to predict the next value
    if include_numeric_value:
        concept_value_decoder_layer = ConceptValuePredictionLayer(
            name='concept_value_decoder_layer'
        )
        concept_values = concept_value_decoder_layer(
            original_concept_embeddings,
            contextualized_embeddings,
            concept_value_masks
        )
        # Creating a new tensor based on the existing one with a new name
        model_outputs.append(tf.identity(concept_values, name='next_value_predictions'))

    model = tf.keras.Model(inputs=model_inputs, outputs=model_outputs)

    return model


def sample_predicted_probability(
        pred_logits,
        top_k,
        temperature
):
    pred_logits, indices = tf.math.top_k(pred_logits / temperature, k=top_k, sorted=True)
    indices = np.asarray(indices).astype("int32")
    preds = tf.keras.activations.softmax(tf.expand_dims(pred_logits, 0))[0]
    preds = np.asarray(preds).astype("float32")
    return np.random.choice(indices, p=preds)


def generate_artificial_time_tokens():
    """
    Generate all the time tokens used in training
    :return:
    """
    day_tokens = [f'D{i}' for i in range(2000)]
    week_tokens = [f'W{i}' for i in range(4)]
    month_tokens = [f'M{i}' for i in range(12)]
    long_term_tokens = ['LT']
    return day_tokens + week_tokens + month_tokens + long_term_tokens


