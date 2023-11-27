# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import operator
import pathlib
from functools import reduce, wraps
from typing import Any, Dict, List, Tuple, Union

import pandas as pd
import transformers
from tqdm import tqdm
from collections import Counter

# Get the global logger:
_LOGGER = logging.getLogger()


def _check_mlrun_and_open_mpi() -> Tuple["mlrun.MLClientCtx", "mpi4py.MPI.Intracomm"]:
    global _LOGGER

    is_mpi = False
    try:
        import mlrun

        context = mlrun.get_or_create_ctx(name="mlrun")
        _LOGGER = context.logger
        is_mpi = context.labels.get("kind", "job") == "mpijob"

        if is_mpi:
            try:
                from mpi4py import MPI

                return context, MPI.COMM_WORLD
            except ModuleNotFoundError as mpi4py_not_found:
                context.logger.error(
                    "To distribute the function using MLRun's 'mpijob' you need to have `mpi4py` package in your "
                    "interpreter. Please run `pip install mpi4py` and make sure you have open-mpi."
                )
                raise mpi4py_not_found
    except ModuleNotFoundError as module_not_found:
        if is_mpi:
            raise module_not_found
    return None, None


def open_mpi_handler(
    worker_inputs: List[str], root_worker_inputs: Dict[str, Any] = None
):
    global _LOGGER

    # Check for MLRun and OpenMPI availability:
    context, comm = _check_mlrun_and_open_mpi()

    def decorator(handler):
        if comm is None or comm.Get_size() == 1:
            return handler

        @wraps(handler)
        def wrapper(**kwargs):
            # Get the open mpi environment properties:
            size = comm.Get_size()
            rank = comm.Get_rank()

            # Give the correct chunk of the workers inputs:
            for worker_input in worker_inputs:
                input_argument = kwargs[worker_input]
                if input_argument is None:
                    continue
                if isinstance(input_argument, str):
                    input_argument = _get_text_files(
                        data_path=pathlib.Path(input_argument).absolute()
                    )
                if len(input_argument) < size:
                    raise ValueError(
                        f"Cannot split the input '{worker_input}' of length {len(input_argument)} to {size} workers. "
                        f"Please reduce the amount of workers for this input."
                    )
                even_chunk_size = len(input_argument) // size
                chunk_start = rank * even_chunk_size
                chunk_end = (
                    (rank + 1) * even_chunk_size
                    if rank + 1 < size
                    else len(input_argument)
                )
                context.logger.info(
                    f"Rank #{rank}: Processing input chunk of '{worker_input}' "
                    f"from index {chunk_start} to {chunk_end}."
                )
                if isinstance(input_argument, list):
                    input_argument = input_argument[chunk_start:chunk_end]
                elif isinstance(input_argument, pd.DataFrame):
                    input_argument = input_argument.iloc[chunk_start:chunk_end:, :]
                kwargs[worker_input] = input_argument

            # Set the root worker only arguments:
            if rank == 0 and root_worker_inputs:
                kwargs.update(root_worker_inputs)

            # Run the worker:
            output = handler(**kwargs)

            # Send the output to the root rank (rank #0):
            output = comm.gather(output, root=0)
            if rank == 0:
                # Join the outputs:
                context.logger.info("Collecting data from workers to root worker.")
                dataframe = pd.concat(objs=[df for df, _ in output], axis=0)
                errors_dictionary = reduce(operator.ior, [err for _, err in output], {})
                return dataframe, errors_dictionary
            return None

        return wrapper

    return decorator


@open_mpi_handler(worker_inputs=["data_path"], root_worker_inputs={"verbose": True})
def answer_questions(
        data_path: Union[str, List[str]],
        model_name: str,
        questions: Union[List[str], List[List[str]]],
        device_map: Union[str, dict] = None,
        model_kwargs: dict = None,
        auto_gptq_exllama_max_input_length: int = None,
        tokenizer_name: str = None,
        tokenizer_kwargs: dict = None,
        text_wrapper: Union[str, List[str]] = "",
        questions_wrapper: Union[str, List[str]] = "",
        generation_config: Union[Dict, List[Dict]] = {},
        questions_config: Union[Dict, List[Dict]] = {},
        batch_size: int = 1,
        questions_columns: List[str] = None,
        verbose: bool = False,
        mapping: Union[List[Dict], List[List[Dict]]] = None,
) -> Tuple[pd.DataFrame, dict]:
    """
    Answer questions with a context to the given text files contents by a pretrained LLM model. Each text file will have
    the following prompt built:

    start of `text_wrapper`
    <text file content>
    end of `text_wrapper`

    start of `questions_wrapper`
    1. <questions[0]>
    2. <questions[1]>
    ...
    n. <questions[n-1]>
    end of `questions_wrapper`

    :param data_path:                          A path to a directory of text files or a path to a text file to ask
                                               questions about.
    :param model_name:                         The pre-trained model name from the huggingface hub to use for asking
                                               questions.
    :param questions:                          The questions to ask.
    :param device_map:                         A map to use for loading the model on multiple devices.
    :param model_kwargs:                       Keyword arguments to pass for loading the model using HuggingFace's
                                               `transformers.AutoModelForCausalLM.from_pretrained` function.
    :param auto_gptq_exllama_max_input_length: For AutoGPTQ models to set and extend the model's input buffer size.
    :param tokenizer_name:                     The tokenizer name from the huggingface hub to use. If not given, the
                                               model name will be used.
    :param tokenizer_kwargs:                   Keyword arguments to pass for loading the tokenizer using HuggingFace's
                                               `transformers.AutoTokenizer.from_pretrained` function.
    :param text_wrapper:                       A wrapper for the file's text. Will be added at the start of the prompt.
                                               Must have a placeholder ('{}') for the text of the file.
    :param questions_wrapper:                  A wrapper for the questions received. Will be added after the text
                                               wrapper in the prompt template. Must have a placeholder ('{}') for the
                                               questions.
    :param generation_config:                  HuggingFace's `GenerationConfig` keyword arguments to pass to the
                                               `generate` method.
    :param questions_config:                   A dictionary or list of dictionaries containing specific ways to answer
                                               questions (using a poll for example).
    :param batch_size:                         Batch size for inference.
    :param questions_columns:                  Columns to use for the dataframe returned.
    :param verbose:                            Whether to present logs of a progress bar and errors. Default: True.
    :param mapping:                            A list of dictionaries mapping string values to numeric values to use
                                               when averaging answers. if more than one questioning type isn't normal,
                                               it is expected to be a list of lists as long as the number of different
                                               question steps given to this function.


    :returns: A tuple of:

              * A dataframe dataset of the questions answers.
              * A dictionary of errored files that were not inferred or were not answered properly.
    """
    global _LOGGER

    # Get the input text files to question:
    if verbose:
        _LOGGER.info("Collecting text files.")
    if isinstance(data_path, str):
        data_path = pathlib.Path(data_path).absolute()
        text_files = _get_text_files(data_path=data_path)
    else:
        text_files = data_path
    if verbose:
        _LOGGER.info(f"Collected {len(text_files)} text files.")

    # Get the prompt template:
    if verbose:
        _LOGGER.info("Creating prompt template.")
    # Organize questions as a list of list, and count number of sub-lists for future use
    number_of_question_steps = 1 if isinstance(questions[0], str) else len(questions)
    questions = _list_me(questions, "questions", number_of_question_steps)
    # Organize prompt parts at proper length
    text_wrapper = _list_me(text_wrapper, "text_wrapper", number_of_question_steps)
    questions_wrapper = _list_me(questions_wrapper, "questions_wrapper", number_of_question_steps)
    # Create a list of prompt according to given parts and questions
    prompt_template = []
    questions = questions if isinstance(questions[0], list) else [questions]
    for i in range(number_of_question_steps):
        prompt_template.append(_get_prompt_template(
            text_wrapper=text_wrapper[i],
            questions_wrapper=questions_wrapper[i],
            questions=questions[i],
        ))
    if verbose:
        _LOGGER.info(f"Prompt template created:\n\n{prompt_template}\n")

    questions_amount = sum([len(sublist) for sublist in questions])
    # Get the questions columns:
    questions_columns = questions_columns or [
        f"q{i}" for i in range(1, questions_amount + 1)
    ]
    if len(questions_columns) != questions_amount:
        raise ValueError(
            f"The provided questions columns length ({len(questions_columns)}) "
            f"does not match the questions amount ({questions_amount})"
        )

    # Load the generation config:
    if verbose:
        _LOGGER.info("Loading generation configuration.")
    generation_config = _list_me(generation_config, "generation_config", number_of_question_steps)
    generation_configs = []
    # load a list of all appropriate configs 
    for cfg in generation_config:
        generation_configs.append(transformers.GenerationConfig(**(cfg or {})))
    if verbose:
        _LOGGER.info(f"Generation configuration loaded: {generation_config}")

    # Load the model and tokenizer into a pipeline object:
    if verbose:
        _LOGGER.info(f"Loading model '{model_name}'.")
    generation_pipeline = _get_generation_pipeline(
        model_name=model_name,
        device_map=device_map,
        tokenizer_name=tokenizer_name or model_name,
        model_kwargs=model_kwargs or {},
        tokenizer_kwargs=tokenizer_kwargs or {},
        auto_gptq_exllama_max_input_length=auto_gptq_exllama_max_input_length,
        batch_size=batch_size,
    )
    if verbose:
        _LOGGER.info("Model loaded.")

    # Prepare the successes dataframe and errors dictionary to be returned:
    successes = []
    errors = {}
    # Split the files into batches:
    file_batches = [
        text_files[i: i + batch_size]
        if i + batch_size < len(text_files)
        else text_files[i:]
        for i in range(0, len(text_files), batch_size)
    ]
    questions_config = _list_me(questions_config, "questions_config", number_of_question_steps)
    # Go over the batches of text files and question them:
    for file_batch in tqdm(
            file_batches,
            desc="Generating answers",
            unit=f"file (batch of {batch_size})",
            disable=not verbose,
    ):
        try:
            total_answers = []
            for step in range(number_of_question_steps):
                current_questions_amount = len(questions[step])
                # Read batch (read the text from the text files):
                batched_input = _read_file_batch(
                    file_batch=file_batch, prompt_template=prompt_template[step]
                )
                if questions_config[step] == {} or questions_config[step]["type"] == "default":
                    # Infer batch:
                    batched_answers = _answer_questions(
                        questions_amount=current_questions_amount,
                        batched_input=batched_input,
                        generation_pipeline=generation_pipeline,
                        generation_config=generation_configs[step],
                    )
                    batched_answers = batched_answers[0]
                elif questions_config[step]["type"] == "poll":
                    votes = []
                    number_voters = (questions_config[step]["poll_count"] or 5)
                    for k in range(number_voters):
                        batched_answers = _answer_questions(
                            questions_amount=current_questions_amount,
                            batched_input=batched_input,
                            generation_pipeline=generation_pipeline,
                            generation_config=generation_configs[step],
                        )
                        votes += batched_answers
                    batched_answers = []
                    if questions_config[step]["poll_strategy"] == "average":
                        for question in current_questions_amount:
                            # create a least of all answers to relevant question
                            answer = [votes[voter][question] for voter in number_voters]
                            # check if mapping just for this question step or for overs
                            current_mapping = mapping if isinstance(mapping[0], dict) else mapping[i]
                            batched_answers.append(_average_answer(answer, mapping=current_mapping[question]))
                    elif questions_config[step]["poll_strategy"] == "most_common":
                        for question in range(current_questions_amount):
                            answer = [votes[voter][question] for voter in range(number_voters)]
                            batched_answers.append(_most_common_answer(answer))
                total_answers += batched_answers
            # Collect it to the successes:
            successes += [
                [file.name, *answers]
                for file, answers in zip(file_batch, [total_answers])
            ]
        except Exception as exception:
            # Note the exception as error in the dictionary:
            batch_file_names = ", ".join([file.name for file in file_batch])
            if verbose:
                _LOGGER.warning(
                    f"Error in batch '{batch_file_names}': {str(exception)}"
                )
            errors[batch_file_names] = str(exception)
            continue

    # Construct the answers dataframe:
    columns = [
        "text_file",
        *questions_columns,
    ]
    successes = pd.DataFrame(
        successes,
        columns=columns,
    )

    # Print the head of the produced dataframe and return:
    if verbose:
        _LOGGER.info(
            f"Done ({successes.shape[0]}/{len(text_files)})\n"
            f"Answers summary:\n"
            f"{successes.head()}"
        )
    return successes, errors


def _get_text_files(
        data_path: pathlib.Path,
) -> List[pathlib.Path]:
    # Check if the path is of a directory or a file:
    if data_path.is_dir():
        # Get all files inside the directory:
        text_files = list(data_path.glob("*.*"))
    elif data_path.is_file():
        text_files = [data_path]
    else:
        raise ValueError(
            f"Unrecognized data path. The parameter `data_path` must be either a directory path or a file path. "
            f"Given: {str(data_path)} "
        )

    return text_files


def _get_prompt_template(
        text_wrapper: str,
        questions_wrapper: str,
        questions: List[str],
) -> str:
    # Validate and build the text wrapper:
    text_wrapper = text_wrapper or (
        "Given the following text:\n" "-----\n" "{}\n" "-----"
    )
    if text_wrapper.count("{}") != 1:
        raise ValueError(
            "The `text_wrapper` must include one placeholder '{}' for the text of the file to be asked about."
        )

    # Validate and build the question wrapper:
    questions_wrapper = questions_wrapper or "Answer the questions:\n" "{}"
    if questions_wrapper.count("{}") != 1:
        raise ValueError(
            "The `questions_wrapper` must include one placeholder '{}' for the list of questions."
        )

    # Validate and parse the questions:
    if len(questions) == 0:
        raise ValueError("Please include at least one question.")
    questions = "\n".join(
        [f"{i}. {question}" for i, question in enumerate(questions, 1)]
    )

    # Construct the template:
    return f"{text_wrapper}\n{questions_wrapper.format(questions)}\n"


def _get_generation_pipeline(
        model_name: str,
        device_map: Union[str, dict],
        tokenizer_name: str,
        model_kwargs: dict,
        tokenizer_kwargs: dict,
        auto_gptq_exllama_max_input_length: int = None,
        batch_size: int = 1,
):
    # Load the model:
    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_name, device_map=device_map, **model_kwargs
    )

    # Set exllama max input length if provided:
    if auto_gptq_exllama_max_input_length:
        from auto_gptq import exllama_set_max_input_length

        model = exllama_set_max_input_length(
            model=model, max_input_length=auto_gptq_exllama_max_input_length
        )

    # Load the tokenizer:
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        tokenizer_name, **tokenizer_kwargs
    )

    # Initialize a generation pipline and return:
    pipe = transformers.pipeline(
        task="text-generation",
        model=model,
        tokenizer=tokenizer,
        batch_size=batch_size,
    )
    pipe.tokenizer.pad_token_id = model.config.eos_token_id
    return pipe


def _read_file_batch(
        file_batch: List[pathlib.Path],
        prompt_template: str,
) -> List[str]:
    batch = []
    for file in file_batch:
        with open(file, "r", encoding='utf-8') as fp:
            batch.append(prompt_template.format(fp.read()))
    return batch


def _get_answers(generated_text: str, questions_amount: int) -> List[str]:
    # Clear answer start (part before numbers):
    if "1." not in generated_text:
        raise ValueError(
            f"Answer 1. is missing from the generated text: '{generated_text}'"
        )
    text = generated_text.split("1.", 1)[1]

    # Start extracting the answers:
    answers = []
    for i in range(1, questions_amount + 1):
        # If it's the last answer to look for, take the rest of the text:
        if i == questions_amount:
            answer_i = text
        # Verify there is a question number in the text:
        elif f"{i + 1}." not in text:
            raise ValueError(
                f"Answer {i + 1}. is missing from the generated text: '{generated_text}'"
            )
        # Take i's answer:
        else:
            answer_i, text = text.split(f"{i + 1}.", 1)
        # Collect the answer removing redundant spaces:
        answers.append(answer_i.strip())

    return answers


def _answer_questions(
        questions_amount: int,
        batched_input: List[str],
        generation_pipeline: transformers.Pipeline,
        generation_config: transformers.GenerationConfig,
) -> List[List[str]]:
    # Infer through the llm:

    batched_output = generation_pipeline(
        batched_input,
        generation_config=generation_config,
        eos_token_id=generation_pipeline.tokenizer.eos_token_id,
        return_full_text=False,
        num_return_sequences=1,
    )
    # Process the outputs to get the answers:
    batched_answers = []
    for output in batched_output:
        # Get the generated answers:
        answers = _get_answers(
            generated_text=output[0]["generated_text"],
            questions_amount=questions_amount,
        )
        # Collect the processed answers:
        batched_answers.append(answers)
    return batched_answers


def _list_me(list_to_check: list, name: str, length: int):
    list_to_check = list_to_check if isinstance(list_to_check, list) else [list_to_check]
    list_len = len(list_to_check)
    if list_len != length:
        if list_len == 1:
            return list_to_check * length
        else:
            raise ValueError(
                f"The argument value of '{name}' is not equal to the length of the given questions - {length}"
            )
    return list_to_check


def _most_common_answer(answers):
    count = Counter(answers)
    most_common = count.most_common(1)
    return most_common[0][0]


def _average_answer(answers, mapping=None):
    if isinstance(answers[0], str):
        if mapping:
            numeric_values = [_map_to_int(answer, mapping) for answer in answers]
        else:
            raise ValueError(
                "Cannot perform poll with average answer strategy of non numeric values without a mapping,"
                " please provide a mapping of string values to integers or choose 'most_common' as strategy."
            )
    else:
        numeric_values = answers
    avg = sum(numeric_values) / len(numeric_values)
    # Round to the closest integer and return corresponding value
    return _map_to_str(round(avg), mapping)


def _map_to_int(answer, mapping):
    try:
        # Try to convert the answer to an integer
        return int(answer)
    except ValueError:
        # If conversion fails, use the provided mapping
        return mapping.get(answer, 0)


def _map_to_str(answer, mapping):
    if not mapping:
        return answer
    return next(key for key, value in mapping.items() if value == answer)
