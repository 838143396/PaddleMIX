# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from enum import Enum
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    TypedDict,
    Union,
)

from datasets import DatasetDict, concatenate_datasets, interleave_datasets

from ...utils.log import logger

if TYPE_CHECKING:
    from datasets import Dataset, IterableDataset
    from paddlenlp.trainer import Seq2SeqTrainingArguments

    from ..common import DatasetAttr
    from .args import DataArguments
    from .plugin import ImageInput, VideoInput


SLOTS = Sequence[Union[str, Set[str], Dict[str, str]]]


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    FUNCTION = "function"
    OBSERVATION = "observation"


class DatasetModule(TypedDict):
    train_dataset: Optional[Union["Dataset", "IterableDataset"]]
    eval_dataset: Optional[Union["Dataset", "IterableDataset"]]


def merge_dataset(
    all_datasets: List[Union["Dataset", "IterableDataset"]], data_args: "DataArguments", seed: int
) -> Union["Dataset", "IterableDataset"]:
    r"""
    Merges multiple datasets to a unified dataset.
    """
    if len(all_datasets) == 1:
        return all_datasets[0]
    elif data_args.mix_strategy == "concat":
        if data_args.streaming:
            logger.warning_once("The samples between different datasets will not be mixed in streaming mode.")

        return concatenate_datasets(all_datasets)
    elif data_args.mix_strategy.startswith("interleave"):
        if not data_args.streaming:
            logger.warning_once("We recommend using `mix_strategy=concat` in non-streaming mode.")

        return interleave_datasets(
            datasets=all_datasets,
            probabilities=data_args.interleave_probs,
            seed=seed,
            stopping_strategy="first_exhausted" if data_args.mix_strategy.endswith("under") else "all_exhausted",
        )
    else:
        raise ValueError(f"Unknown mixing strategy: {data_args.mix_strategy}.")


def split_dataset(
    dataset: Union["Dataset", "IterableDataset"], data_args: "DataArguments", seed: int
) -> "DatasetDict":
    r"""
    Splits the dataset and returns a dataset dict containing train set and validation set.

    Supports both map dataset and iterable dataset.
    """
    if data_args.streaming:
        dataset = dataset.shuffle(buffer_size=data_args.buffer_size, seed=seed)
        val_set = dataset.take(int(data_args.val_size))
        train_set = dataset.skip(int(data_args.val_size))
        return DatasetDict({"train": train_set, "validation": val_set})
    else:
        val_size = int(data_args.val_size) if data_args.val_size > 1 else data_args.val_size
        dataset = dataset.train_test_split(test_size=val_size, seed=seed)
        return DatasetDict({"train": dataset["train"], "validation": dataset["test"]})


def _convert_images(
    images: Union["ImageInput", Sequence["ImageInput"]],
    dataset_attr: "DatasetAttr",
    data_args: "DataArguments",
) -> Optional[List["ImageInput"]]:
    r"""
    Optionally concatenates image path to dataset dir when loading from local disk.
    """
    if not isinstance(images, list):
        images = [images]
    elif len(images) == 0:
        return None
    else:
        images = images[:]

    if dataset_attr.load_from in ["script", "file"]:
        for i in range(len(images)):
            if isinstance(images[i], str) and os.path.isfile(os.path.join(data_args.image_dir, images[i])):
                images[i] = os.path.join(data_args.image_dir, images[i])

    return images


def _convert_videos(
    videos: Union["VideoInput", Sequence["VideoInput"]],
    dataset_attr: "DatasetAttr",
    data_args: "DataArguments",
) -> Optional[List["VideoInput"]]:
    r"""
    Optionally concatenates video path to dataset dir when loading from local disk.
    """
    if not isinstance(videos, list):
        videos = [videos]
    elif len(videos) == 0:
        return None
    else:
        videos = videos[:]

    if dataset_attr.load_from in ["script", "file"]:
        for i in range(len(videos)):
            if isinstance(videos[i], str) and os.path.isfile(os.path.join(data_args.image_dir, videos[i])):
                videos[i] = os.path.join(data_args.image_dir, videos[i])

    return videos


def convert_alpaca(
    example: Dict[str, Any],
    dataset_attr: "DatasetAttr",
    data_args: "DataArguments",
) -> Dict[str, Any]:
    r"""
    Converts alpaca format dataset to the standard format.
    """
    prompt = []
    if dataset_attr.history and isinstance(example[dataset_attr.history], list):
        for old_prompt, old_response in example[dataset_attr.history]:
            prompt.append({"role": Role.USER.value, "content": old_prompt})
            prompt.append({"role": Role.ASSISTANT.value, "content": old_response})

    query = []
    if dataset_attr.prompt and example[dataset_attr.prompt]:
        query.append(example[dataset_attr.prompt])

    if dataset_attr.query and example[dataset_attr.query]:
        query.append(example[dataset_attr.query])

    prompt.append({"role": Role.USER.value, "content": "\n".join(query)})  # "prompt\nquery"

    if dataset_attr.kto_tag and isinstance(example[dataset_attr.kto_tag], bool):  # kto example
        response = [{"role": Role.ASSISTANT.value, "content": example[dataset_attr.response]}]
        if example[dataset_attr.kto_tag]:
            response = response + [{"role": Role.ASSISTANT.value, "content": ""}]
        else:
            response = [{"role": Role.ASSISTANT.value, "content": ""}] + response
    elif (
        dataset_attr.ranking
        and isinstance(example[dataset_attr.chosen], str)
        and isinstance(example[dataset_attr.rejected], str)
    ):  # pairwise example
        response = [
            {"role": Role.ASSISTANT.value, "content": example[dataset_attr.chosen]},
            {"role": Role.ASSISTANT.value, "content": example[dataset_attr.rejected]},
        ]
    elif dataset_attr.response and isinstance(example[dataset_attr.response], str):  # normal example
        response = [{"role": Role.ASSISTANT.value, "content": example[dataset_attr.response]}]
    else:  # unsupervised
        response = []

    convert_images = partial(_convert_images, dataset_attr=dataset_attr, data_args=data_args)
    convert_videos = partial(_convert_videos, dataset_attr=dataset_attr, data_args=data_args)
    output = {
        "_prompt": prompt,
        "_response": response,
        "_system": example[dataset_attr.system] if dataset_attr.system else "",
        "_tools": example[dataset_attr.tools] if dataset_attr.tools else "",
        "_images": convert_images(example[dataset_attr.images]) if dataset_attr.images else None,
        "_videos": convert_videos(example[dataset_attr.videos]) if dataset_attr.videos else None,
    }
    return output


def convert_sharegpt(
    example: Dict[str, Any],
    dataset_attr: "DatasetAttr",
    data_args: "DataArguments",
) -> Dict[str, Any]:
    r"""
    Converts sharegpt format dataset to the standard format.
    """
    tag_mapping = {
        dataset_attr.user_tag: Role.USER.value,
        dataset_attr.assistant_tag: Role.ASSISTANT.value,
        dataset_attr.observation_tag: Role.OBSERVATION.value,
        dataset_attr.function_tag: Role.FUNCTION.value,
        dataset_attr.system_tag: Role.SYSTEM.value,
    }
    odd_tags = (dataset_attr.user_tag, dataset_attr.observation_tag)
    even_tags = (dataset_attr.assistant_tag, dataset_attr.function_tag)
    accept_tags = (odd_tags, even_tags)
    messages = example[dataset_attr.messages]
    if (
        dataset_attr.system_tag
        and len(messages) != 0
        and messages[0][dataset_attr.role_tag] == dataset_attr.system_tag
    ):
        system = messages[0][dataset_attr.content_tag]
        messages = messages[1:]
    else:
        system = example[dataset_attr.system] if dataset_attr.system else ""

    aligned_messages = []
    broken_data = False
    for turn_idx, message in enumerate(messages):
        if message[dataset_attr.role_tag] not in accept_tags[turn_idx % 2]:
            logger.warning_rank0(f"Invalid role tag in {messages}.")
            broken_data = True

        aligned_messages.append(
            {"role": tag_mapping[message[dataset_attr.role_tag]], "content": message[dataset_attr.content_tag]}
        )

    if (not dataset_attr.ranking and len(aligned_messages) % 2 != 0) or (
        dataset_attr.ranking and len(aligned_messages) % 2 == 0
    ):
        logger.warning_rank0(f"Invalid message count in {messages}.")
        broken_data = True

    if dataset_attr.kto_tag and isinstance(example[dataset_attr.kto_tag], bool):  # kto example
        prompt = aligned_messages[:-1]
        response = aligned_messages[-1:]
        if example[dataset_attr.kto_tag]:
            response = response + [{"role": Role.ASSISTANT.value, "content": ""}]
        else:
            response = [{"role": Role.ASSISTANT.value, "content": ""}] + response
    elif (
        dataset_attr.ranking
        and isinstance(example[dataset_attr.chosen], dict)
        and isinstance(example[dataset_attr.rejected], dict)
    ):  # pairwise example
        chosen = example[dataset_attr.chosen]
        rejected = example[dataset_attr.rejected]
        if (
            chosen[dataset_attr.role_tag] not in accept_tags[-1]
            or rejected[dataset_attr.role_tag] not in accept_tags[-1]
        ):
            logger.warning_rank0(f"Invalid role tag in {[chosen, rejected]}.")
            broken_data = True

        prompt = aligned_messages
        response = [
            {"role": tag_mapping[chosen[dataset_attr.role_tag]], "content": chosen[dataset_attr.content_tag]},
            {"role": tag_mapping[rejected[dataset_attr.role_tag]], "content": rejected[dataset_attr.content_tag]},
        ]
    else:  # normal example
        prompt = aligned_messages[:-1]
        response = aligned_messages[-1:]

    if broken_data:
        logger.warning_rank0("Skipping this abnormal example.")
        prompt, response = [], []

    convert_images = partial(_convert_images, dataset_attr=dataset_attr, data_args=data_args)
    convert_videos = partial(_convert_videos, dataset_attr=dataset_attr, data_args=data_args)
    output = {
        "_prompt": prompt,
        "_response": response,
        "_system": system,
        "_tools": example[dataset_attr.tools] if dataset_attr.tools else "",
        "_images": convert_images(example[dataset_attr.images]) if dataset_attr.images else None,
        "_videos": convert_videos(example[dataset_attr.videos]) if dataset_attr.videos else None,
    }
    return output


def align_dataset(
    dataset: Union["Dataset", "IterableDataset"],
    dataset_attr: "DatasetAttr",
    data_args: "DataArguments",
    training_args: "Seq2SeqTrainingArguments",
) -> Union["Dataset", "IterableDataset"]:
    r"""
    Aligned dataset:
        _prompt: [{"role": "user", "content": "..."}] * (2T - 1)
        _response: [{"role": "assistant", "content": "..."}] * N (N > 1 for ranking dataset)
        _system: "..."
        _tools: "...",
        _images: [],
        _videos: [],
    """
    if dataset_attr.formatting == "alpaca":
        convert_func = partial(convert_alpaca, dataset_attr=dataset_attr, data_args=data_args)
    else:
        convert_func = partial(convert_sharegpt, dataset_attr=dataset_attr, data_args=data_args)

    column_names = list(next(iter(dataset)).keys())
    kwargs = {}
    if not data_args.streaming:
        # kwargs = dict(
        #     num_proc=data_args.preprocessing_num_workers,
        #     load_from_cache_file=(not data_args.overwrite_cache) or (training_args.local_process_index != 0),
        #     desc="Converting format of dataset",
        # )
        kwargs = dict(
            num_proc=1,
            load_from_cache_file=False,
            desc="Converting format of dataset",
        )
    return dataset.map(
        convert_func,
        batched=False,
        remove_columns=column_names,
        **kwargs,
    )


def has_tokenized_data(path: "os.PathLike") -> bool:
    r"""
    Checks if the path has a tokenized dataset.
    """
    return os.path.isdir(path) and len(os.listdir(path)) > 0
