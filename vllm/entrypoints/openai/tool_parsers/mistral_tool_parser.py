import json
import re
from typing import Dict, List, Optional, Union

import partial_json_parser
from partial_json_parser.core.options import Allow
from transformers import (AutoTokenizer, PreTrainedTokenizer,
                          PreTrainedTokenizerFast)

from vllm.entrypoints.openai.protocol import (DeltaFunctionCall, DeltaMessage,
                                              DeltaToolCall,
                                              ExtractedToolCallInformation,
                                              FunctionCall,
                                              InitialDeltaToolCall, ToolCall)
from vllm.entrypoints.openai.tool_parsers.abstract_tool_parser import (
    ToolParser)
from vllm.entrypoints.openai.tool_parsers.utils import (
    extract_intermediate_diff)
from vllm.logger import init_logger

logger = init_logger(__name__)


class MistralToolParser(ToolParser):
    """
    Tool call parser for Mistral 7B Instruct v0.3, intended for use with the
    examples/tool_chat_template_mistral.jinja template. There are several
    IMPORTANT CAVEATS for this parser:
        - The chat template is NOT official and does not work well if you try to
         get the model to call 2+ tools at once without temperature=0.
            Stick to only one tool call per generation, or set temp to 0
            as the chat template is not reliable with > 1 and the model
            Will lose coherence.
        - Mistral's tool call format, that this translates into an OpenAI
            format, uses SINGLE QUOTES which cannot be parsed to JSON. To enable
            JSON parsing and serialization, we find-and-replace these with
            DOUBLE QUOTES. To prevent tool call corruption / deserialization
            failure, ensure that your tool calls and in particular your
            ARGUMENTS never contain single or double quotes except as JSON
            control characters.

    Used when --enable-api-tools --enable-auto-tool-choice --tool-call-parser
    mistral are all set
    """

    # the bot_token is the token indicating tool call(s) follow. Tokens before
    # this token will be parsed as content; and
    # if not present, the entire response will be parsed as text content.
    bot_token: str = "[TOOL_CALLS]"  # string literal
    bot_token_id: int = 5  # token ID thereof from the models" tokenizer
    tool_call_regex = re.compile(r"\[{.*?}\]", re.DOTALL)

    @staticmethod
    def extract_tool_calls(model_output: str) -> ExtractedToolCallInformation:
        """
        Extract the tool calls from a complete model response. Requires
        find-and-replacing single quotes with double quotes for JSON parsing,
        make sure your tool call arguments don't ever include quotes!
        """

        logger.debug(
            "Trying to extract mistral tool calls from the following:")
        logger.debug(model_output)
        # Get the tool call token from the tokenizer
        if MistralToolParser.bot_token not in model_output:
            return ExtractedToolCallInformation(tools_called=False,
                                                tool_calls=[],
                                                content=model_output)
        else:
            try:

                # this will throw an exception if we can't find the tool call
                # properly
                raw_tool_call = MistralToolParser.tool_call_regex.findall(
                    model_output.replace(MistralToolParser.bot_token,
                                         "")  # remove BOT token
                    .replace("'", "\"")  # replace string quotes
                )[0]

                # load the JSON, and then use it to build the Function and
                # Tool Call
                function_call_arr = json.loads(raw_tool_call)
                tool_calls: List[ToolCall] = [
                    ToolCall(
                        type="function",
                        function=FunctionCall(
                            name=raw_function_call["name"],
                            # function call args are JSON but as a string
                            arguments=json.dumps(
                                raw_function_call["arguments"])))
                    for raw_function_call in function_call_arr
                ]
                content = model_output.split(MistralToolParser.bot_token)[0]
                return ExtractedToolCallInformation(
                    tools_called=True,
                    tool_calls=tool_calls,
                    content=content if len(content) > 0 else None)

            except Exception as e:
                logger.error("Error in extracting tool call from response: %s",
                             e)
                print("ERROR", e)
                # return information to just treat the tool call as regular JSON
                return ExtractedToolCallInformation(tools_called=False,
                                                    tool_calls=[],
                                                    content=model_output)

    def __init__(self,
                 tokenizer: Optional[Union[PreTrainedTokenizer,
                                           PreTrainedTokenizerFast,
                                           PreTrainedTokenizerFast,
                                           AutoTokenizer]] = None):
        super().__init__(tokenizer)

        # initialize properties used for state when parsing tool calls in
        # streaming mode
        self.prev_tool_call_arr: List[Dict] = []
        self.current_tool_id: int = -1
        self.current_tool_name_sent: bool = False
        self.current_tool_initial_sent: bool = False
        self.streamed_args_for_tool: List[str] = [
        ]  # map what has been streamed for each tool so far to a list

    def extract_tool_calls_streaming(
        self,
        previous_text: str,
        current_text: str,
        delta_text: str,
        previous_token_ids: List[int],
        current_token_ids: List[int],
        delta_token_ids: List[int],
    ) -> Union[DeltaMessage, None]:

        # if the tool call token is not in the tokens generated so far, append
        # output to contents since it's not a tool
        if self.bot_token_id not in current_token_ids:
            return DeltaMessage(content=delta_text)

        # if the tool call token ID IS in the tokens generated so far, that
        # means we're parsing as tool calls now
        else:

            # handle if we detected the BOT token which means the start of tool
            # calling
            if (self.bot_token_id in delta_token_ids
                    and len(delta_token_ids) == 1):
                # if it's the only token, return None, so we don't send a chat
                # completion any don't send a control token
                return None

            # bit mask flags for partial JSON parsing. If the name hasn't been
            # sent yet, don't allow sending
            # an incomplete string since OpenAI only ever (as far as I have
            # seen) allows sending the entire tool/ function name at once.
            flags = Allow.ALL if self.current_tool_name_sent \
                else Allow.ALL & ~Allow.STR
            try:

                # replace BOT token with empty string, and convert single quotes
                # to double to allow parsing as JSON since mistral uses single
                # quotes instead of double for tool calls
                tool_call_message_portion = current_text.split(
                    self.bot_token)[1]
                parsable_arr = tool_call_message_portion.replace("\'", "\"")

                # logger.debug('parsing: %s', parsable_arr)

                # tool calls are generated in an array, so do partial JSON
                # parsing on the entire array
                tool_call_arr: List[Dict] = partial_json_parser.loads(
                    parsable_arr, flags)

                # select as the current tool call the one we're on the state at
                current_tool_call: Dict = tool_call_arr[self.current_tool_id]

                # case: we are starting a new tool in the array
                #   -> array has > 0 length AND length has moved past cursor
                if len(tool_call_arr) > 0 and len(
                        tool_call_arr) > self.current_tool_id + 1:

                    # if we're moving on to a new call, first make sure we
                    # haven't missed anything in the previous one that was
                    # auto-generated due to JSON completions, but wasn't
                    # streamed to the client yet.
                    if self.current_tool_id >= 0:
                        diff: Union[str,
                                    None] = current_tool_call.get("arguments")
                        if diff:
                            diff = json.dumps(diff).replace(
                                self.streamed_args_for_tool[
                                    self.current_tool_id], "")
                            delta = DeltaMessage(tool_calls=[
                                DeltaToolCall(index=self.current_tool_id,
                                              function=DeltaFunctionCall(
                                                  arguments=diff).model_dump(
                                                      exclude_none=True))
                            ])
                            self.streamed_args_for_tool[
                                self.current_tool_id] += diff
                        else:
                            delta = None
                    else:
                        delta = None
                    # re-set stuff pertaining to progress in the current tool
                    self.current_tool_id = len(tool_call_arr) - 1
                    self.current_tool_name_sent = False
                    self.current_tool_initial_sent = False
                    self.streamed_args_for_tool.append("")
                    logger.debug("starting on new tool %d",
                                 self.current_tool_id)
                    return delta

                # case: update an existing tool - this is handled below
                elif len(
                        tool_call_arr
                ) - 1 == self.current_tool_id and self.current_tool_id >= 0:
                    pass

                # if there is NOTHING in the array, e.g. if only the open
                # bracket was streamed yet
                else:
                    return None

                # if the current tool initial data incl. the id, type=function
                # and idx not sent, send that
                if not self.current_tool_initial_sent:
                    logger.debug("Sending InitialDeltaToolCall")
                    self.current_tool_initial_sent = True
                    delta = DeltaMessage(tool_calls=[
                        InitialDeltaToolCall(
                            index=self.current_tool_id).model_dump(
                                exclude_none=True)
                    ])

                # if the current tool name hasn't been sent, send if available
                # - otherwise no chunks
                elif not self.current_tool_name_sent:
                    function_name = current_tool_call.get("name")
                    if function_name:
                        logger.debug(
                            "Sending DeltaToolCall with function name %s",
                            function_name)
                        delta = DeltaMessage(tool_calls=[
                            DeltaToolCall(index=self.current_tool_id,
                                          function=DeltaFunctionCall(
                                              name=function_name).model_dump(
                                                  exclude_none=True))
                        ])
                        self.current_tool_name_sent = True
                    else:
                        delta = None

                # now we know we're on the same tool call and we're streaming
                # arguments
                else:

                    prev_arguments = self.prev_tool_call_arr[
                        self.current_tool_id].get("arguments")
                    cur_arguments = current_tool_call.get("arguments")

                    new_text = delta_text.replace("\'", "\"")

                    if not cur_arguments and not prev_arguments:

                        delta = None
                    elif not cur_arguments and prev_arguments:
                        logger.error(
                            "INVARIANT - impossible to have arguments reset "
                            "mid-arguments")
                        delta = None
                    elif cur_arguments and not prev_arguments:
                        cur_arguments_json = json.dumps(cur_arguments)
                        logger.debug("finding %s in |%s|", new_text,
                                     cur_arguments_json)

                        arguments_delta = cur_arguments_json[:
                                                             cur_arguments_json
                                                             .index(new_text) +
                                                             len(new_text)]
                        logger.debug("First tokens in arguments received: %s",
                                     arguments_delta)
                        delta = DeltaMessage(tool_calls=[
                            DeltaToolCall(index=self.current_tool_id,
                                          function=DeltaFunctionCall(
                                              arguments=arguments_delta).
                                          model_dump(exclude_none=True))
                        ])
                        self.streamed_args_for_tool[
                            self.current_tool_id] += arguments_delta

                    elif cur_arguments and prev_arguments:
                        cur_args_json = json.dumps(cur_arguments)
                        prev_args_json = json.dumps(prev_arguments)
                        logger.debug("Searching for diff between \n%s\n%s",
                                     cur_args_json, prev_args_json)

                        argument_diff = extract_intermediate_diff(
                            cur_args_json, prev_args_json)
                        logger.debug("got arguments diff: %s", argument_diff)
                        delta = DeltaMessage(tool_calls=[
                            DeltaToolCall(index=self.current_tool_id,
                                          function=DeltaFunctionCall(
                                              arguments=argument_diff).
                                          model_dump(exclude_none=True))
                        ])
                        self.streamed_args_for_tool[
                            self.current_tool_id] += argument_diff
                    else:
                        # try parsing it with regular JSON - if it works we're
                        # at the end, and we need to send the difference between
                        # tokens streamed so far and the valid JSON
                        delta = None

                # check to see if the name is defined and has been sent. if so,
                # stream the name - otherwise keep waiting
                # finish by setting old and returning None as base case
                self.prev_tool_call_arr = tool_call_arr
                return delta

            except Exception as e:
                logger.error("Error trying to handle streaming tool call: %s",
                             e)
                logger.debug(
                    "Skipping chunk as a result of tool streaming extraction "
                    "error")
                return None