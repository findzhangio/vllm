import time
import json
from typing import (AsyncGenerator, AsyncIterator, Awaitable, Dict, List,
                    Optional, Type)
from typing import (AsyncGenerator, AsyncIterator, Awaitable, Dict, List,
                    Optional, Union, Sequence as GenericSequence)
from typing import Union
from fastapi import Request
from transformers import PreTrainedTokenizer

from vllm.config import ModelConfig
from vllm.engine.protocol import AsyncEngineClient
from vllm.entrypoints.chat_utils import (ConversationMessage,
                                         load_chat_template,
                                         parse_chat_message_content,
                                         ConversationMessage)
from vllm.entrypoints.logger import RequestLogger
from vllm.entrypoints.openai.protocol import (
    ChatCompletionLogProb, ChatCompletionLogProbs,
    ChatCompletionLogProbsContent, ChatCompletionNamedToolChoiceParam,
    ChatCompletionRequest, ChatCompletionResponse,
    ChatCompletionResponseChoice, ChatCompletionResponseStreamChoice,
    ChatCompletionStreamResponse, ChatMessage, DeltaMessage, ErrorResponse,
    FunctionCall, ToolCall, UsageInfo, DeltaToolCall, DeltaFunctionCall)
from vllm.entrypoints.openai.serving_engine import (LoRAModulePath,
                                                    OpenAIServing,
                                                    PromptAdapterPath)
from vllm.inputs import PromptInputs
from vllm.logger import init_logger
from vllm.multimodal import MultiModalDataDict
from vllm.outputs import RequestOutput
from vllm.sequence import Logprob
from vllm.tracing import (contains_trace_headers, extract_trace_headers,
                          log_tracing_disabled_warning)
from vllm.utils import random_uuid

from vllm.entrypoints.openai.tool_parsers import (ToolParser,
                                                  MistralToolParser,
                                                  Hermes2ProToolParser)

from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(loader=FileSystemLoader('./'),
                  autoescape=select_autoescape())

logger = init_logger(__name__)


class OpenAIServingChat(OpenAIServing):

    def __init__(self,
                 async_engine_client: AsyncEngineClient,
                 model_config: ModelConfig,
                 served_model_names: List[str],
                 response_role: str,
                 *,
                 lora_modules: Optional[List[LoRAModulePath]],
                 prompt_adapters: Optional[List[PromptAdapterPath]],
                 request_logger: Optional[RequestLogger],
                 chat_template: Optional[str],
                 return_tokens_as_token_ids: bool = False,
                 enable_auto_tools: Optional[bool] = False,
                 tool_parser: Optional[str] = None):
        super().__init__(async_engine_client=async_engine_client,
                         model_config=model_config,
                         served_model_names=served_model_names,
                         lora_modules=lora_modules,
                         prompt_adapters=prompt_adapters,
                         request_logger=request_logger,
                         return_tokens_as_token_ids=return_tokens_as_token_ids)

        self.response_role = response_role
        self.use_tool_use_model_template = False
        self.chat_template = load_chat_template(chat_template)

        # set up tool use
        self.enable_auto_tools: bool = enable_auto_tools or False
        if self.enable_auto_tools:
            logger.info(
                '"Auto" tool choice has been enabled please note that while the '
                'parallel_tool_calls client option is preset for compatibility '
                'reasons, it will be ignored.')

        self.tool_parser: Optional[Type[ToolParser]] = None
        if self.enable_auto_tools:
            if tool_parser == 'mistral':
                self.tool_parser = MistralToolParser
            elif tool_parser == 'hermes':
                self.tool_parser = Hermes2ProToolParser
            else:
                raise TypeError(
                    'Error: --enable-auto-tool-choice requires --tool-parser')

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        raw_request: Optional[Request] = None
    ) -> Union[ErrorResponse, AsyncGenerator[str, None],
               ChatCompletionResponse]:
        """Completion API similar to OpenAI's API.

        See https://platform.openai.com/docs/api-reference/chat/create
        for the API specification. This API mimics the OpenAI
        ChatCompletion API.

        """
        error_check_ret = await self._check_model(request)
        if error_check_ret is not None:
            print('Error with model', error_check_ret)
            return error_check_ret

        try:
            (
                lora_request,
                prompt_adapter_request,
            ) = self._maybe_get_adapters(request)

            model_config = self.model_config
            tokenizer = await self.async_engine_client.get_tokenizer(
                lora_request)

            conversation: List[ConversationMessage] = []
            mm_futures: List[Awaitable[MultiModalDataDict]] = []

            for msg in request.messages:
                chat_parsed_result = parse_chat_message_content(
                    msg, model_config, tokenizer)

                conversation.extend(chat_parsed_result.messages)
                mm_futures.extend(chat_parsed_result.mm_futures)

            tool_dicts = None if request.tools is None else [
                tool.model_dump() for tool in request.tools
            ]

            prompt = tokenizer.apply_chat_template(
                conversation=conversation,
                tokenize=False,
                add_generation_prompt=request.add_generation_prompt,
                tools=tool_dicts,
                documents=request.documents,
                chat_template=request.chat_template or self.chat_template,
                **(request.chat_template_kwargs or {}),
            )
        except Exception as e:
            logger.error("Error in applying chat template from request: %s", e)
            return self.create_error_response(str(e))

        mm_data: Optional[MultiModalDataDict] = None

        try:
            if len(mm_futures):
                # since we support only single mm data currently
                if len(mm_futures) != 1:
                    return self.create_error_response("Multiple 'image_url' input is currently not supported.")
                mm_data = await mm_futures[0]
        except Exception as e:
            logger.error("Error in loading multi-modal data: %s", e)
            return self.create_error_response(str(e))

        # validation for OpenAI tools
            # tool_choice = "required" is not supported
        if request.tool_choice != 'required':
            return self.create_error_response('tool_choice = "required" is not supported!')

            # "auto" tools requires --enable-api-tools --enable-auto-tool-choice and --tool-parser
        if request.tool_choice == 'auto' and not (self.enable_auto_tools and self.tool_parser is not None):
            return self.create_error_response(
                  '"auto" tool choice requires --enable-auto-tool-choice and --tool-parser to be set')

        request_id = f"chat-{random_uuid()}"
        try:

            guided_decode_logits_processor = (
                await self._guided_decode_logits_processor(request, tokenizer))

            prompt_inputs = self._tokenize_prompt_input(
                request,
                tokenizer,
                prompt,
                truncate_prompt_tokens=request.truncate_prompt_tokens,
                add_special_tokens=request.add_special_tokens,
            )

            sampling_params = request.to_sampling_params(
                tokenizer,
                guided_decode_logits_processor,
                default_max_tokens=self.max_model_len -
                len(prompt_inputs["prompt_token_ids"]))

            self._log_inputs(request_id,
                             prompt_inputs,
                             params=sampling_params,
                             lora_request=lora_request,
                             prompt_adapter_request=prompt_adapter_request)

            engine_inputs: PromptInputs = {
                "prompt_token_ids": prompt_inputs["prompt_token_ids"],
            }
            if mm_data is not None:
                engine_inputs["multi_modal_data"] = mm_data

            is_tracing_enabled = (
                await self.async_engine_client.is_tracing_enabled())
            trace_headers = None
            if is_tracing_enabled and raw_request:
                trace_headers = extract_trace_headers(raw_request.headers)
            if (not is_tracing_enabled and raw_request
                    and contains_trace_headers(raw_request.headers)):
                log_tracing_disabled_warning()

            result_generator = self.async_engine_client.generate(
                engine_inputs,
                sampling_params,
                request_id,
                lora_request=lora_request,
                trace_headers=trace_headers,
                prompt_adapter_request=prompt_adapter_request,
            )
        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            return self.create_error_response(str(e))

        # Streaming response
        if request.stream:
            return self.chat_completion_stream_generator(
                request, result_generator, request_id, conversation, tokenizer)
        else:
            try:
                generator = await self.chat_completion_full_generator(
                    request, raw_request, result_generator, request_id,
                    conversation, tokenizer)

                if not isinstance(generator, ChatCompletionResponse):
                    raise ValueError('Expected generator to be instance of ChatCompletionResponse')
                return generator

            except ValueError as e:
                # TODO: Use a vllm-specific Validation Error
                return self.create_error_response(str(e))

    def get_chat_request_role(self, request: ChatCompletionRequest) -> str:
        if request.add_generation_prompt:
            return self.response_role
        else:
            return request.messages[-1]["role"]

    async def chat_completion_stream_generator(
        self,
        request: ChatCompletionRequest,
        result_generator: AsyncIterator[RequestOutput],
        request_id: str,
        conversation: List[ConversationMessage],
        tokenizer: PreTrainedTokenizer,
    ) -> AsyncGenerator[str, None]:
        model_name = self.served_model_names[0]
        created_time = int(time.time())
        chunk_object_type = "chat.completion.chunk"
        first_iteration = True

        # Send response for each token for each request.n (index)
        num_choices = 1 if request.n is None else request.n
        previous_texts = [""] * num_choices
        previous_num_tokens = [0] * num_choices
        finish_reason_sent = [False] * num_choices

        tool_parser: Optional[ToolParser] = self.tool_parser(
            tokenizer) if self.tool_parser else None

        try:
            async for res in result_generator:
                # We need to do it here, because if there are exceptions in
                # the result_generator, it needs to be sent as the FIRST
                # response (by the try...catch).
                if first_iteration:
                    # Send first response for each request.n (index) with
                    # the role
                    role = self.get_chat_request_role(request)
                    for i in range(num_choices):
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i,
                            delta=DeltaMessage(role=role),
                            logprobs=None,
                            finish_reason=None)
                        chunk = ChatCompletionStreamResponse(
                            id=request_id,
                            object=chunk_object_type,
                            created=created_time,
                            choices=[choice_data],
                            model=model_name)
                        if (request.stream_options
                                and request.stream_options.include_usage):
                            if (request.stream_options.continuous_usage_stats):
                                prompt_tokens = len(res.prompt_token_ids)
                                usage = UsageInfo(prompt_tokens=prompt_tokens,
                                                  completion_tokens=0,
                                                  total_tokens=prompt_tokens)
                                chunk.usage = usage
                            else:
                                chunk.usage = None

                        data = chunk.model_dump_json(exclude_unset=True)
                        yield f"data: {data}\n\n"

                    # Send response to echo the input portion of the
                    # last message
                    if request.echo:
                        last_msg_content = ""
                        if conversation and conversation[-1].get(
                                "content") and conversation[-1].get(
                                    "role") == role:
                            last_msg_content = conversation[-1]["content"] or ''

                        if last_msg_content:
                            for i in range(num_choices):
                                choice_data = (
                                    ChatCompletionResponseStreamChoice(
                                        index=i,
                                        delta=DeltaMessage(
                                            content=last_msg_content),
                                        logprobs=None,
                                        finish_reason=None))
                                chunk = ChatCompletionStreamResponse(
                                    id=request_id,
                                    object=chunk_object_type,
                                    created=created_time,
                                    choices=[choice_data],
                                    model=model_name)
                                if (request.stream_options and
                                        request.stream_options.include_usage):
                                    if (request.stream_options.
                                            continuous_usage_stats):
                                        prompt_tokens = len(
                                            res.prompt_token_ids)
                                        usage = UsageInfo(
                                            prompt_tokens=prompt_tokens,
                                            completion_tokens=0,
                                            total_tokens=prompt_tokens)
                                        chunk.usage = usage
                                    else:
                                        chunk.usage = None

                                data = chunk.model_dump_json(
                                    exclude_unset=True)
                                yield f"data: {data}\n\n"
                    first_iteration = False

                for output in res.outputs:

                    i = output.index

                    if finish_reason_sent[i]:
                        continue

                    delta_token_ids = output.token_ids[previous_num_tokens[i]:]
                    out_logprobs = output.logprobs[
                        previous_num_tokens[i]:] if output.logprobs else None

                    if request.logprobs and request.top_logprobs is not None:
                        assert out_logprobs is not None, (
                            "Did not output logprobs")
                        logprobs = self._create_chat_logprobs(
                            token_ids=delta_token_ids,
                            top_logprobs=out_logprobs,
                            tokenizer=tokenizer,
                            num_output_top_logprobs=request.top_logprobs,
                        )
                    else:
                        logprobs = None

                    delta_text = output.text[len(previous_texts[i]):]
                    delta_message: Optional[DeltaMessage] = None

                    # handle streaming deltas for tools with tool_choice
                    if request.tool_choice and type(
                            request.tool_choice
                    ) is ChatCompletionNamedToolChoiceParam:
                        delta_message = DeltaMessage(tool_calls=[
                            ToolCall(function=FunctionCall(
                                name=request.tool_choice.function.name,
                                arguments=delta_text))
                        ])

                    # handle streaming deltas for tools with tool_choice
                    elif (request.tools and tool_parser
                          and (request.tool_choice is None
                               or request.tool_choice == 'auto')
                          and self.enable_auto_tools):

                        delta_message = tool_parser.extract_tool_calls_streaming(
                            previous_text=previous_texts[i],
                            current_text=output.text,
                            delta_text=delta_text,
                            previous_token_ids=output.token_ids[:-1 * len(delta_token_ids)],
                            current_token_ids=output.token_ids,
                            delta_token_ids=delta_token_ids)
                    else:
                        delta_message = DeltaMessage(content=delta_text)

                    # handle setting the previous values for the next iteration
                    previous_texts[i] = output.text
                    previous_num_tokens[i] = len(output.token_ids)

                    # if the message delta is None (e.g. because it was a "control token" for tool calls, then
                    #   get the next token without streaming a chunk
                    if delta_message is None:
                        continue

                    if output.finish_reason is None:
                        # Send token-by-token response for each request.n

                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i,
                            delta=delta_message,
                            logprobs=logprobs,
                            finish_reason=None)
                        chunk = ChatCompletionStreamResponse(
                            id=request_id,
                            object=chunk_object_type,
                            created=created_time,
                            choices=[choice_data],
                            model=model_name)
                        if (request.stream_options
                                and request.stream_options.include_usage):
                            if (request.stream_options.continuous_usage_stats):
                                prompt_tokens = len(res.prompt_token_ids)
                                completion_tokens = len(output.token_ids)
                                usage = UsageInfo(
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens,
                                    total_tokens=prompt_tokens +
                                    completion_tokens,
                                )
                                chunk.usage = usage
                            else:
                                chunk.usage = None

                        data = chunk.model_dump_json(exclude_unset=True)
                        yield f"data: {data}\n\n"
                    else:
                        # check to make sure we haven't "forgotten" to stream
                        #   any tokens that were generated but previously
                        #   matched by partial json parsing
                        if (delta_message.tool_calls
                                and delta_message.tool_calls[0]
                                and delta_message.tool_calls[0].function and
                            (delta_message.tool_calls[0].function.arguments
                             == '' or
                             delta_message.tool_calls[0].function.arguments and
                             (output.finish_reason == 'stop'
                              or output.finish_reason == 'tool_calls'))
                                and tool_parser):
                            expected_call = json.dumps(
                                tool_parser.prev_tool_call_arr[
                                    len(tool_parser.prev_tool_call_arr) -
                                    1].get('arguments', {}))
                            actual_call = tool_parser.streamed_args_for_tool[
                                len(tool_parser.prev_tool_call_arr) - 1]
                            remaining_call = expected_call.replace(
                                actual_call, '', 1)
                            delta_message = DeltaMessage(tool_calls=[
                                DeltaToolCall(
                                    index=len(tool_parser.prev_tool_call_arr) -
                                    1,
                                    function=DeltaFunctionCall(
                                        arguments=remaining_call).model_dump(
                                            exclude_none=True))
                            ])
                        # Send the finish response for each request.n only once
                        prompt_tokens = len(res.prompt_token_ids)
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i,
                            delta=delta_message,
                            logprobs=logprobs,
                            finish_reason=output.finish_reason
                            if not (tool_parser
                                    and len(tool_parser.prev_tool_call_arr))
                            else 'tool_calls',
                            stop_reason=output.stop_reason)
                        chunk = ChatCompletionStreamResponse(
                            id=request_id,
                            object=chunk_object_type,
                            created=created_time,
                            choices=[choice_data],
                            model=model_name)
                        if (request.stream_options
                                and request.stream_options.include_usage):
                            if (request.stream_options.continuous_usage_stats):
                                prompt_tokens = len(res.prompt_token_ids)
                                completion_tokens = len(output.token_ids)
                                usage = UsageInfo(
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens,
                                    total_tokens=prompt_tokens +
                                    completion_tokens,
                                )
                                chunk.usage = usage
                            else:
                                chunk.usage = None
                        data = chunk.model_dump_json(exclude_unset=True)
                        yield f"data: {data}\n\n"
                        finish_reason_sent[i] = True

            # once the final token is handled, if stream_options.include_usage is sent, send the usage
            if (request.stream_options
                    and request.stream_options.include_usage):
                final_usage = UsageInfo(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=previous_num_tokens[i],
                    total_tokens=prompt_tokens + previous_num_tokens[i],
                )

                final_usage_chunk = ChatCompletionStreamResponse(
                    id=request_id,
                    object=chunk_object_type,
                    created=created_time,
                    choices=[],
                    model=model_name,
                    usage=final_usage)
                final_usage_data = (final_usage_chunk.model_dump_json(
                    exclude_unset=True, exclude_none=True))
                yield f"data: {final_usage_data}\n\n"

        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            data = self.create_streaming_error_response(str(e))
            yield f"data: {data}\n\n"
        # Send the final done message after all response.n are finished
        yield "data: [DONE]\n\n"

    async def chat_completion_full_generator(
        self,
        request: ChatCompletionRequest,
        raw_request: Optional[Request],
        result_generator: AsyncIterator[RequestOutput],
        request_id: str,
        conversation: List[ConversationMessage],
        tokenizer: PreTrainedTokenizer,
    ) -> Union[ErrorResponse, ChatCompletionResponse]:

        model_name = self.served_model_names[0]
        created_time = int(time.time())
        final_res: Optional[RequestOutput] = None

        async for res in result_generator:
            if raw_request is not None and await raw_request.is_disconnected():
                # Abort the request if the client disconnects.
                await self.async_engine_client.abort(request_id)
                return self.create_error_response("Client disconnected")
            final_res = res
        assert final_res is not None

        choices: List[ChatCompletionResponseChoice] = []

        role = self.get_chat_request_role(request)
        for output in final_res.outputs:
            token_ids = output.token_ids
            out_logprobs = output.logprobs

            if request.logprobs and request.top_logprobs is not None:
                assert out_logprobs is not None, "Did not output logprobs"
                logprobs = self._create_chat_logprobs(
                    token_ids=token_ids,
                    top_logprobs=out_logprobs,
                    num_output_top_logprobs=request.top_logprobs,
                    tokenizer=tokenizer,
                )
            else:
                logprobs = None

            # by default, tools are not used.
            tools_called = False

            # if auto tools are not enabled, and a named tool choice using
            #   outlines is not being used
            if not (self.enable_auto_tools
                    or not self.tool_parser) and not isinstance(
                        request.tool_choice,
                        ChatCompletionNamedToolChoiceParam):
                message = ChatMessage(role=role, content=output.text)

            # if the request uses tools and specified a tool choice
            elif request.tool_choice and type(
                    request.tool_choice) is ChatCompletionNamedToolChoiceParam:

                message = ChatMessage(
                    role=role,
                    content="",
                    tool_calls=[
                        ToolCall(function=FunctionCall(
                            name=request.tool_choice.function.name,
                            arguments=output.text))
                    ])
                tools_called = True

            # if the request doesn't use tool choice OR specifies to not use a tool
            elif not request.tool_choice or request.tool_choice == "none":

                message = ChatMessage(role=role, content=output.text)

            # handle when there are tools and tool choice is auto
            elif request.tools and (
                    request.tool_choice == "auto"
                    or request.tool_choice is None) and self.enable_auto_tools \
                    and self.tool_parser:

                tool_call_info = self.tool_parser.extract_tool_calls(
                    output.text)
                tools_called = tool_call_info.tools_called
                if tool_call_info.tools_called:
                    message = ChatMessage(role=role,
                                          content=tool_call_info.content,
                                          tool_calls=tool_call_info.tool_calls)

                else:
                    # FOR NOW make it a chat message; we will have to detect the type to make it later.
                    message = ChatMessage(role=role, content=output.text)

            # undetermined case that is still important to handle
            else:
                logger.error(
                    'Error in chat_completion_full_generator - cannot determine if tools should '
                    'be extracted. Returning a standard chat completion.')
                message = ChatMessage(role=role, content=output.text)

            choice_data = ChatCompletionResponseChoice(
                index=output.index,
                message=message,
                logprobs=logprobs,
                finish_reason='tool_calls' if tools_called else
                output.finish_reason if output.finish_reason else 'stop',
                stop_reason=output.stop_reason)
            choices.append(choice_data)

        if request.echo:
            last_msg_content = ""
            if conversation and conversation[-1].get(
                    "content") and conversation[-1].get("role") == role:
                last_msg_content = conversation[-1]["content"] or ''

            for choice in choices:
                full_message = last_msg_content + choice.message.content if choice.message.content else ''
                choice.message.content = full_message

        num_prompt_tokens = len(final_res.prompt_token_ids)
        num_generated_tokens = sum(
            len(output.token_ids) for output in final_res.outputs)
        usage = UsageInfo(
            prompt_tokens=num_prompt_tokens,
            completion_tokens=num_generated_tokens,
            total_tokens=num_prompt_tokens + num_generated_tokens,
        )
        response = ChatCompletionResponse(
            id=request_id,
            created=created_time,
            model=model_name,
            choices=choices,
            usage=usage,
        )

        return response

    def _get_top_logprobs(
            self, logprobs: Dict[int, Logprob], top_logprobs: Optional[int],
            tokenizer: PreTrainedTokenizer) -> List[ChatCompletionLogProb]:
        return [
            ChatCompletionLogProb(token=(token := self._get_decoded_token(
                p[1],
                p[0],
                tokenizer,
                return_as_token_id=self.return_tokens_as_token_ids)),
                                  logprob=max(p[1].logprob, -9999.0),
                                  bytes=list(
                                      token.encode("utf-8", errors="replace")))
            for i, p in enumerate(logprobs.items())
            if top_logprobs and i < top_logprobs
        ]

    def _create_chat_logprobs(
        self,
        token_ids: GenericSequence[int],
        top_logprobs: GenericSequence[Optional[Dict[int, Logprob]]],
        tokenizer: PreTrainedTokenizer,
        num_output_top_logprobs: Optional[int] = None,
    ) -> ChatCompletionLogProbs:
        """Create OpenAI-style logprobs."""

        logprobs_content = []

        for i, token_id in enumerate(token_ids):
            step_top_logprobs = top_logprobs[i]
            if step_top_logprobs is None:
                token = tokenizer.decode(token_id)
                if self.return_tokens_as_token_ids:
                    token = f"token_id:{token_id}"
                logprobs_content.append(
                    ChatCompletionLogProbsContent(
                        token=token,
                        bytes=list(token.encode("utf-8", errors="replace"))))
            else:
                logprobs_content.append(
                    ChatCompletionLogProbsContent(
                        token=self._get_decoded_token(
                            step_top_logprobs[token_id], token_id, tokenizer,
                            self.return_tokens_as_token_ids),
                        logprob=max(step_top_logprobs[token_id].logprob,
                                    -9999.0),
                        bytes=list(
                            step_top_logprobs[token_id].decoded_token.encode(
                                "utf-8", errors="replace")),
                        top_logprobs=self._get_top_logprobs(
                            step_top_logprobs, num_output_top_logprobs,
                            tokenizer)))

        return ChatCompletionLogProbs(content=logprobs_content)
