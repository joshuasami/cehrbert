import argparse
import datetime
import os
import random
import uuid
from typing import Any, Dict

import torch
from models.hf_models.tokenization_hf_cehrgpt import CehrGptTokenizer
from transformers import GenerationConfig
from models.hf_models.hf_cehrgpt import CEHRGPT2LMHeadModel

import pandas as pd

from models.gpt_model import TopKStrategy, TopPStrategy, TopMixStrategy


def generate_single_batch(
        model,
        tokenizer,
        batch_size,
        demographic_info,
        max_new_tokens=512,
        mini_num_of_concepts=1,
        top_p=0.95,
        top_k=50,
        temperature=1.0,
        repetition_penalty=1.0,
        num_beams=1,
        num_beam_groups=1,
        epsilon_cutoff=0.0,
        device: Any = 'cpu'
) -> Dict[str, Any]:
    random_prompts = random.sample(
        demographic_info,
        batch_size
    )

    with torch.no_grad():
        generation_config = GenerationConfig(
            repetition_penalty=repetition_penalty,
            max_length=max_new_tokens,
            min_length=mini_num_of_concepts,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            bos_token_id=tokenizer.end_token_id,
            eos_token_id=tokenizer.end_token_id,
            pad_token_id=tokenizer.pad_token_id,
            do_sample=True,
            use_cache=True,
            return_dict_in_generate=True,
            output_attentions=False,
            output_hidden_states=False,
            output_scores=False,
            renormalize_logits=True,
            num_beams=num_beams,
            num_beam_groups=num_beam_groups,
            epsilon_cutoff=epsilon_cutoff
        )
        batched_prompts = torch.tensor(random_prompts).to(device)
        results = model.generate(
            inputs=batched_prompts,
            generation_config=generation_config,
        )

    sequences = [tokenizer.decode(seq.cpu().numpy()) for seq in results.sequences]
    value_indicators = None
    values = None
    if results.sequence_val_masks is not None:
        value_indicators = [m[:len(s)] for m, s in zip(results.sequence_val_masks.detach().cpu().numpy(), sequences)]
    if results.sequence_vals is not None:
        values = [v[:len(s)] for v, s in zip(results.sequence_vals.detach().cpu().numpy(), sequences)]

    return {
        'sequences': sequences,
        'value_indicators': value_indicators,
        'values': values
    }


def main(
        args
):
    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")

    cehrgpt_tokenizer = CehrGptTokenizer.from_pretrained(args.tokenizer_folder)
    cehrgpt_model = CEHRGPT2LMHeadModel.from_pretrained(args.model_folder).eval().to(device)

    if args.sampling_strategy == TopKStrategy.__name__:
        folder_name = f'top_k{args.top_k}'
        args.top_p = 1.0
    elif args.sampling_strategy == TopPStrategy.__name__:
        folder_name = f'top_p{int(args.top_p * 1000)}'
        args.top_k = cehrgpt_tokenizer.vocab_size
    elif args.sampling_strategy == TopMixStrategy.__name__:
        folder_name = f'top_mix_p{int(args.top_p * 1000)}_k{args.top_k}'
    else:
        raise RuntimeError(
            'sampling_strategy has to be one of the following two options TopKStrategy or TopPStrategy'
        )

    if args.temperature != 1.0:
        folder_name = f'{folder_name}_temp_{int(args.temperature * 1000)}'

    if args.repetition_penalty != 1.0:
        folder_name = f'{folder_name}_repetition_penalty_{int(args.repetition_penalty * 1000)}'

    if args.num_beams > 1:
        folder_name = f'{folder_name}_num_beams_{int(args.num_beams)}'

    if args.num_beam_groups > 1:
        folder_name = f'{folder_name}_num_beam_groups_{int(args.num_beam_groups)}'

    if args.epsilon_cutoff > 0.0:
        folder_name = f'{folder_name}_epsilon_cutoff_{int(args.epsilon_cutoff * 10000)}'

    output_folder_name = os.path.join(
        args.output_folder,
        folder_name,
        'generated_sequences'
    )

    if not os.path.exists(output_folder_name):
        os.makedirs(output_folder_name)

    # atexit.register(strategy._extended._collective_ops._pool.close)  # type: ignore
    # atexit.register(strategy._extended._cross_device_ops._pool.close) # type: ignore
    # atexit.register(strategy._extended._host_cross_device_ops._pool.close) #type: ignore
    print(f'{datetime.datetime.now()}: Loading tokenizer at {args.model_folder}')
    print(f'{datetime.datetime.now()}: Loading model at {args.model_folder}')
    print(f'{datetime.datetime.now()}: Write sequences to {output_folder_name}')
    print(f'{datetime.datetime.now()}: Context window {args.context_window}')
    print(f'{datetime.datetime.now()}: Temperature {args.temperature}')
    print(f'{datetime.datetime.now()}: Repetition Penalty {args.repetition_penalty}')
    print(f'{datetime.datetime.now()}: Sampling Strategy {args.sampling_strategy}')
    print(f'{datetime.datetime.now()}: Num beam {args.num_beams}')
    print(f'{datetime.datetime.now()}: Num beam groups {args.num_beam_groups}')
    print(f'{datetime.datetime.now()}: Epsilon cutoff {args.epsilon_cutoff}')
    print(f'{datetime.datetime.now()}: Top P {args.top_p}')
    print(f'{datetime.datetime.now()}: Top K {args.top_k}')
    print(f'{datetime.datetime.now()}: Loading demographic_info at {args.demographic_data_path}')

    data = pd.read_parquet(
        args.demographic_data_path
    )
    # data = data[data.num_of_concepts >= args.min_num_of_concepts]
    demographic_info = data.concept_ids.apply(lambda concept_list: concept_list[0:4])
    demographic_info = [cehrgpt_tokenizer.encode(_) for _ in demographic_info]

    num_of_batches = args.num_of_patients // args.batch_size + 1
    sequence_to_flush = []
    current_person_id = 1
    for i in range(num_of_batches):
        print(f'{datetime.datetime.now()}: Batch {i} started')
        batch_sequences = generate_single_batch(
            cehrgpt_model,
            cehrgpt_tokenizer,
            args.batch_size,
            demographic_info,
            max_new_tokens=args.context_window,
            mini_num_of_concepts=args.min_num_of_concepts,
            top_p=args.top_p,
            top_k=args.top_k,
            temperature=args.temperature,
            repetition_penalty=args.repetition_penalty,
            num_beams=args.num_beams,
            num_beam_groups=args.num_beam_groups,
            epsilon_cutoff=args.epsilon_cutoff,
            device=device
        )

        # Clear the cache
        torch.cuda.empty_cache()

        for seq, value_indicator, value in zip(
                batch_sequences["sequences"], batch_sequences["value_indicators"], batch_sequences["values"]
        ):
            output = {
                'concept_ids': seq,
                'person_id': current_person_id
            }
            if value is not None:
                output['concept_values'] = value
            if value_indicator is not None:
                output['concept_value_masks'] = value_indicator

            sequence_to_flush.append(output)
            current_person_id += 1

        if len(sequence_to_flush) >= args.buffer_size:
            print(f'{datetime.datetime.now()}: Flushing to the Disk at Batch {i}')
            pd.DataFrame(
                sequence_to_flush,
                columns=['concept_ids', 'person_id', 'concept_values', 'concept_value_masks']
            ).to_parquet(os.path.join(output_folder_name, f'{uuid.uuid4()}.parquet'))
            sequence_to_flush.clear()

    if len(sequence_to_flush) > 0:
        print(f'{datetime.datetime.now()}: Flushing to the Disk at Final Batch')
        pd.DataFrame(
            sequence_to_flush,
            columns=['concept_ids']
        ).to_parquet(os.path.join(output_folder_name, f'{uuid.uuid4()}-last.parquet'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Arguments for generating patient sequences')

    parser.add_argument(
        '--tokenizer_folder',
        dest='tokenizer_folder',
        action='store',
        help='The path for your model_folder',
        required=True
    )
    parser.add_argument(
        '--model_folder',
        dest='model_folder',
        action='store',
        help='The path for your model_folder',
        required=True
    )
    parser.add_argument(
        '--output_folder',
        dest='output_folder',
        action='store',
        help='The path for your generated data',
        required=True
    )
    parser.add_argument(
        '--num_of_patients',
        dest='num_of_patients',
        action='store',
        type=int,
        help='The number of patients that will be generated',
        required=True
    )
    parser.add_argument(
        '--batch_size',
        dest='batch_size',
        action='store',
        type=int,
        help='batch_size',
        required=True
    )
    parser.add_argument(
        '--buffer_size',
        dest='buffer_size',
        action='store',
        type=int,
        default=100,
        help='buffer_size',
        required=False
    )
    parser.add_argument(
        '--context_window',
        dest='context_window',
        action='store',
        type=int,
        help='The context window of the gpt model',
        required=True
    )
    parser.add_argument(
        '--min_num_of_concepts',
        dest='min_num_of_concepts',
        action='store',
        type=int,
        default=1,
        required=False
    )
    parser.add_argument(
        '--sampling_strategy',
        dest='sampling_strategy',
        action='store',
        choices=[TopPStrategy.__name__, TopKStrategy.__name__, TopMixStrategy.__name__],
        help='Pick the sampling strategy between top_k and top_p',
        required=True
    )
    parser.add_argument(
        '--top_k',
        dest='top_k',
        action='store',
        default=100,
        type=int,
        help='The number of top concepts to sample',
        required=False
    )
    parser.add_argument(
        '--top_p',
        dest='top_p',
        action='store',
        default=1.0,
        type=float,
        help='The accumulative probability of top concepts to sample',
        required=False
    )
    parser.add_argument(
        '--demographic_data_path',
        dest='demographic_data_path',
        action='store',
        help='The path for your concept_path',
        required=True
    )
    parser.add_argument(
        '--temperature',
        dest='temperature',
        action='store',
        default=1.0,
        type=float,
        help='The temperature parameter for softmax',
        required=False
    )
    parser.add_argument(
        '--repetition_penalty',
        dest='repetition_penalty',
        action='store',
        default=1.0,
        type=float,
        help='The repetition penalty during decoding',
        required=False
    )
    parser.add_argument(
        '--num_beams',
        dest='num_beams',
        action='store',
        default=1,
        type=int,
        required=False
    )
    parser.add_argument(
        '--num_beam_groups',
        dest='num_beam_groups',
        action='store',
        default=1,
        type=int,
        required=False
    )
    parser.add_argument(
        '--epsilon_cutoff',
        dest='epsilon_cutoff',
        action='store',
        default=0.0,
        type=float,
        required=False
    )
    main(parser.parse_args())