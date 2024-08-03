from vllm.entrypoints.openai.protocol import (ToolCall, FunctionCall,
                                              ExtractedToolCallInformation,
                                              DeltaToolCall,
                                              InitialDeltaToolCall,
                                              DeltaFunctionCall, DeltaMessage)
from vllm.logger import init_logger
from typing import List, Dict, Optional, Union
from transformers import (AutoTokenizer, PreTrainedTokenizer,
                          PreTrainedTokenizerFast)
import json
import partial_json_parser
from partial_json_parser import Allow
import re
from vllm.entrypoints.openai.protocol import DeltaMessage

logger = init_logger(__name__)


def find_common_prefix(s1: str, s2: str) -> str:
    """
    Finds a common prefix that is shared between two strings, if there is one.
    Order of arguments is NOT important.

    This function is provided as a UTILITY for extracting information from JSON
    generated by partial_json_parser, to help in ensuring that the right tokens
    are returned in streaming, so that close-quotes, close-brackets and
    close-braces are not returned prematurely.

    e.g. find_common_prefix('{"fruit": "ap"}', '{"fruit": "apple"}') ->
    '{"fruit": "ap'
    """
    prefix = ''
    min_length = min(len(s1), len(s2))
    for i in range(0, min_length):
        if s1[i] == s2[i]:
            prefix += s1[i]
        else:
            break
    return prefix


def find_common_suffix(s1: str, s2: str) -> str:
    """
    Finds a common suffix shared between two strings, if there is one. Order of
    arguments is NOT important.
    Stops when the suffix ends OR it hits an alphanumeric character

    e.g. find_common_suffix('{"fruit": "ap"}', '{"fruit": "apple"}') -> '"}'
    """
    suffix = ''
    min_length = min(len(s1), len(s2))
    for i in range(1, min_length + 1):
        if s1[-i] == s2[-i] and not s1[-i].isalnum():
            suffix = s1[-i] + suffix
        else:
            break
    return suffix


def extract_intermediate_diff(curr: str, old: str) -> str:
    """
    Given two strings, extract the difference in the middle between two strings
    that are known to have a common prefix and/or suffix.

    This function is provided as a UTILITY for extracting information from JSON
    generated by partial_json_parser, to help in ensuring that the right tokens
    are returned in streaming, so that close-quotes, close-brackets and
    close-braces are not returned prematurely. The order of arguments IS
    important - the new version of the partially-parsed JSON must be the first
    argument, and the secnod argument must be from the previous generation.

    What it returns, is tokens that should be streamed to the client.

    e.g. extract_intermediate_diff('{"fruit": "apple"}', '{"fruit": "ap"}')
        -> 'ple'

    """
    suffix = find_common_suffix(curr, old)

    # prevent double-counting
    s2_old = old
    old = old[::-1].replace(suffix[::-1], '', 1)[::-1]
    prefix = find_common_prefix(curr, old)
    diff = curr
    if len(suffix):
        diff = diff[::-1].replace(suffix[::-1], '', 1)[::-1]

    if len(prefix):
        diff = diff.replace(
            prefix, '',
            1)  # replace the prefix only once in case it's mirrored

    return diff


def find_all_indices(string, substring):
    """
    Find all (starting) indices of a substring in a given string. Useful for
    tool call extraction
    """
    indices = []
    index = -1
    while True:
        index = string.find(substring, index + 1)
        if index == -1:
            break
        indices.append(index)
    return indices


class ToolParser:
    """
    Abstract ToolParser class that should not be used directly. Provided
    properties and methods should be used in
    derived classes.
    """

    def __init__(self,
                 tokenizer: Optional[Union[PreTrainedTokenizer,
                                           PreTrainedTokenizerFast,
                                           PreTrainedTokenizerFast,
                                           AutoTokenizer]] = None):
        self.prev_tool_call_arr: List[Dict] = []
        # the index of the tool call that is currently being parsed
        self.current_tool_id: int = -1
        self.current_tool_name_sent: bool = False
        self.current_tool_initial_sent: bool = False
        self.streamed_args_for_tool: List[str] = []

        self.model_tokenizer = tokenizer

    @staticmethod
    def extract_tool_calls(model_output: str) -> ExtractedToolCallInformation:
        """
        Static method that should be implemented for extracting tool calls from
        a complete model-generated string.
        Used for non-streaming responses where we have the entire model response
        available before sending to the client.
        Static because it's stateless.
        """
        raise NotImplementedError(
            'AbstractToolParser.extract_tool_calls has not been implemented!')

    def extract_tool_calls_streaming(
        self,
        previous_text: str,
        current_text: str,
        delta_text: str,
        previous_token_ids: List[int],
        current_token_ids: List[int],
        delta_token_ids: List[int],
    ) -> Union[DeltaMessage, None]:
        """
        Instance method that should be implemented for extracting tool calls
        from an incomplete response; for use when handling tool calls and
        streaming. Has to be an instance method because  it requires state -
        the current text/ tokens/diffs, but also the information about what has
        previously been  parsed and extracted (see constructor)
        """
        raise NotImplementedError(
            'AbstractToolParser.extract_tool_calls_streaming has not been '
            'implemented!')


class MistralToolParser(ToolParser):
    """
    Tool call parser for Mistral 7B Instruct v0.3, intended for use with the examples/tool_chat_template_mistral.jinja
    template. There are server IMPORTANT CAVEATS for this parser:
        - The chat template is NOT official and does not work well if you try to get the model to call 2+ tools at once.
            Stick to only one tool call per generation, as the chat template is not reliable with > 1 and the model
            Will lose coherence.
        - Mistral's tool call format, that this translates into an OpenAI format, uses SINGLE QUOTES which cannot be
            parsed to JSON. To enable JSON parsing and serialization, we find-and-replace these with DOUBLE QUOTES. To
            prevent tool call corruption / deserialization failure, ensure that your tool calls and in particular your
            ARGUMENTS never contain single or double quotes except as JSON control characters.

    Used when --enable-api-tools --enable-auto-tool-choice --tool-call-parser mistral are all set
    """

    # the bot_token is the token indicating tool call(s) follow. Tokens before this token will be parsed as content; and
    # if not present, the entire response will be parsed as text content.
    bot_token: str = '[TOOL_CALLS]'  # string literal
    bot_token_id: int = 5  # token ID thereof from the models' tokenizer
    tool_call_regex = re.compile(r'\[{.*?}\]', re.DOTALL)

    @staticmethod
    def extract_tool_calls(model_output: str) -> ExtractedToolCallInformation:
        """
        Extract the tool calls from a complete model response. Requires find-and-replacing single quotes with double
        quotes for JSON parsing, make sure your tool call arguments don't ever include quotes!
        """

        logger.debug(
            'Trying to extract mistral tool calls from the following:')
        logger.debug(model_output)
        # Get the tool call token from the tokenizer
        if MistralToolParser.bot_token not in model_output:
            return ExtractedToolCallInformation(tools_called=False,
                                                tool_calls=[],
                                                content=model_output)
        else:
            try:

                # this will throw an exception if we can't find the tool call properly
                raw_tool_call = MistralToolParser.tool_call_regex.findall(
                    model_output.replace(MistralToolParser.bot_token,
                                         '')  # remove BOT token
                    .replace("'", '"')  # replace string quotes
                )[0]

                # load the JSON, and then use it to build the Function and Tool Call
                function_call_arr = json.loads(raw_tool_call)
                tool_calls: List[ToolCall] = [
                    ToolCall(
                        type='function',
                        function=FunctionCall(
                            name=raw_function_call['name'],
                            # function call args are JSON but as a string
                            arguments=json.dumps(
                                raw_function_call['arguments'])))
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
                print('ERROR', e)
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

        # initialize properties used for state when parsing tool calls in streaming mode
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

        # if the tool call token is not in the tokens generated so far, append output to contents since it's not a tool
        if self.bot_token_id not in current_token_ids:
            return DeltaMessage(content=delta_text)

        # if the tool call token ID IS in the tokens generated so far, that means we're parsing as tool calls now
        else:

            # handle if we detected the BOT token which means the start of tool calling
            if self.bot_token_id in delta_token_ids:
                # if it's the only token, return None, so we don't send a chat completion any don't send a control token
                if len(delta_token_ids) == 1:
                    return None

            # bit mask flags for partial JSON parsing. If the name hasn't been sent yet, don't allow sending
            # an incomplete string since OpenAI only ever (as far as I have seen) allows sending the entire tool/
            # function name at once.
            flags = Allow.ALL if self.current_tool_name_sent else Allow.ALL & ~Allow.STR
            try:

                # replace BOT token with empty string, and convert single quotes to double to allow parsing as JSON
                # since mistral uses single quotes instead of double for tool calls
                tool_call_message_portion = current_text.split(
                    self.bot_token)[1]
                parsable_arr = tool_call_message_portion.replace('\'', '"')

                logger.debug('parsing: %s', parsable_arr)

                # tool calls are generated in an array, so do partial JSON parsing on the entire array
                tool_call_arr: List[Dict] = partial_json_parser.loads(
                    parsable_arr, flags)

                # select as the current tool call the one we're on the state at
                current_tool_call: Dict = tool_call_arr[self.current_tool_id]

                # case: we are starting a new tool in the array
                #   -> array has nonzero length AND length has moved past curscor
                if len(tool_call_arr) > 0 and len(
                        tool_call_arr) > self.current_tool_id + 1:

                    # if we're moving on to a new call, first make sure we haven't missed anything in the previous
                    # one that was auto-generated due to JSON completions, but wasn't streamed to the client yet.
                    if self.current_tool_id >= 0:
                        diff: Union[str,
                                    None] = current_tool_call.get('arguments')
                        if diff:
                            diff = json.dumps(diff).replace(
                                self.streamed_args_for_tool[
                                    self.current_tool_id], '')
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
                    self.streamed_args_for_tool.append('')
                    logger.debug('starting on new tool %d',
                                 self.current_tool_id)
                    return delta

                # case: update an existing tool - this is handled below
                elif len(
                        tool_call_arr
                ) - 1 == self.current_tool_id and self.current_tool_id >= 0:
                    # logger.debug('update to tool %d', self.current_tool_id)
                    pass

                # if there is NOTHING in the array, e.g. if only the open bracket was streamed yet
                else:
                    logger.debug('No tool call detected yet!')
                    return None

                # if the current tool initial data incl. the id, type=function and idx not sent, send that
                if not self.current_tool_initial_sent:
                    logger.debug('Sending InitialDeltaToolCall')
                    self.current_tool_initial_sent = True
                    delta = DeltaMessage(tool_calls=[
                        InitialDeltaToolCall(
                            index=self.current_tool_id).model_dump(
                                exclude_none=True)
                    ])

                # if the current tool name hasn't been sent, send if available - otherwise no chunks
                elif not self.current_tool_name_sent:
                    function_name = current_tool_call.get('name')
                    if function_name:
                        logger.debug(
                            f'Sending DeltaToolCall with function name {function_name}!'
                        )
                        delta = DeltaMessage(tool_calls=[
                            DeltaToolCall(index=self.current_tool_id,
                                          function=DeltaFunctionCall(
                                              name=function_name).model_dump(
                                                  exclude_none=True))
                        ])
                        self.current_tool_name_sent = True
                    else:
                        delta = None

                # now we know we're on the same tool call and we're streaming arguments
                else:

                    prev_arguments = self.prev_tool_call_arr[
                        self.current_tool_id].get('arguments')
                    cur_arguments = current_tool_call.get('arguments')

                    new_text = delta_text.replace('\'', '"')

                    if not cur_arguments and not prev_arguments:
                        logger.debug(
                            f'Skipping text {new_text} (tokens {delta_token_ids}) - no arguments yet'
                        )
                        delta = None
                    elif not cur_arguments and prev_arguments:
                        logger.error(
                            'INVARIANT - impossible to have arguments reset mid-arguments'
                        )
                        delta = None
                    elif cur_arguments and not prev_arguments:
                        cur_arguments_json = json.dumps(cur_arguments)
                        logger.debug(
                            f'Finding {new_text} in |{cur_arguments_json}|')
                        arguments_delta = cur_arguments_json[:
                                                             cur_arguments_json
                                                             .index(new_text) +
                                                             len(new_text)]
                        logger.debug(
                            f'First tokens in arguments received: {arguments_delta}'
                        )
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
                        logger.debug(
                            f'Searching for diff between \n{cur_args_json}\n{prev_args_json}'
                        )
                        argument_diff = extract_intermediate_diff(
                            cur_args_json, prev_args_json)
                        logger.debug(f'got arguments diff: {argument_diff}')
                        delta = DeltaMessage(tool_calls=[
                            DeltaToolCall(index=self.current_tool_id,
                                          function=DeltaFunctionCall(
                                              arguments=argument_diff).
                                          model_dump(exclude_none=True))
                        ])
                        self.streamed_args_for_tool[
                            self.current_tool_id] += argument_diff
                    else:
                        # try parsing it with regular JSON - if it works we're at the end, and we need to send the
                        #   difference between tokens streamed so far and the valid JSON
                        delta = None

                # check to see if the name is defined and has been sent. if so, stream the name - otherwise keep waiting
                # finish by setting old and returning None as base case
                self.prev_tool_call_arr = tool_call_arr
                return delta

            except Exception as e:
                logger.error(
                    f'Error trying to handle streaming tool call: {e}')
                logger.debug(
                    'Skipping chunk as a result of tool streaming extraction error'
                )
                return None


class Hermes2ProToolParser(ToolParser):
    tool_call_start_token: str = '<tool_call>'
    tool_call_end_token: str = '</tool_call>'

    # regex to match between <tool_call> and </tool_call> OR between <tool_call> and EOS (happens sometimes :))
    tool_call_regex = re.compile(
        r'<tool_call>(.*?)</tool_call>|<tool_call>(.*)', re.DOTALL)
    scratch_pad_regex = re.compile(r'<scratch_pad>(.*?)</scratch_pad>',
                                   re.DOTALL)

    @staticmethod
    def extract_tool_calls(model_output: str) -> ExtractedToolCallInformation:

        # sanity check; avoid unnecessary processing
        if Hermes2ProToolParser.tool_call_start_token not in model_output:
            return ExtractedToolCallInformation(tools_called=False,
                                                tool_calls=[],
                                                content=model_output)

        else:

            try:
                # there are two possible captures - between tags, or between a tag and end-of-string so the result of
                # findall is an array of tuples where one is a function call and the other is None
                function_call_tuples = Hermes2ProToolParser.tool_call_regex.findall(
                    model_output)

                # load the JSON, and then use it to build the Function and Tool Call
                raw_function_calls = [
                    json.loads(match[0] if match[0] else match[1])
                    for match in function_call_tuples
                ]
                tool_calls = [
                    ToolCall(
                        type='function',
                        function=FunctionCall(
                            name=function_call['name'],
                            # function call args are JSON but as a string
                            arguments=json.dumps(function_call['arguments'])))
                    for function_call in raw_function_calls
                ]

                content = model_output[:model_output.find(
                    Hermes2ProToolParser.tool_call_start_token)]
                return ExtractedToolCallInformation(
                    tools_called=True,
                    tool_calls=tool_calls,
                    content=content if content else None)

            except Exception as e:
                logger.error("Error in extracting tool call from response %s",
                             e)
                return ExtractedToolCallInformation(tools_called=False,
                                                    tool_calls=[],
                                                    content=model_output)

    def __init__(self,
                 tokenizer: Optional[Union[PreTrainedTokenizer,
                                           PreTrainedTokenizerFast,
                                           PreTrainedTokenizerFast,
                                           AutoTokenizer]] = None):
        super().__init__(tokenizer)
        self.current_tool_name_sent: bool = False  # reset each time we encounter a new tool in the array
        self.prev_tool_call_arr: List[Dict] = []
        self.current_tool_id: int = -1
        self.current_tool_name_sent: bool = False
        self.current_tool_initial_sent: bool = False
        self.streamed_args_for_tool: List[str] = [
        ]  # map what has been streamed for each tool so far to a list

        if not self.model_tokenizer:
            raise ValueError(
                'The model tokenizer must be passed to the ToolParser constructor during construction.'
            )
        self.tool_call_start_token_id: int = self.model_tokenizer.vocab[
            '<tool_call>']
        self.tool_call_end_token_id: int = self.model_tokenizer.vocab[
            '</tool_call>']
        if not self.tool_call_start_token_id or not self.tool_call_end_token_id:
            raise RuntimeError(
                'Hermes 2 Pro Tool parser could not locate tool call start/end tokens in the tokenizer!'
            )

    def extract_tool_calls_streaming(
            self, previous_text: str, current_text: str, delta_text: str,
            previous_token_ids: List[int], current_token_ids: List[int],
            delta_token_ids: List[int]) -> Union[DeltaMessage, None]:

        logger.debug(f'delta_text: {delta_text}')
        logger.debug(f'delta_token_ids: {delta_token_ids}')
        # check to see if we should be streaming a tool call - is there a
        if self.tool_call_start_token_id not in current_token_ids:
            logger.debug('No tool call tokens found!')
            return DeltaMessage(content=delta_text)

        else:
            try:

                # figure out where we are in the parsing by counting tool call start & end tags
                prev_tool_start_count = previous_token_ids.count(
                    self.tool_call_start_token_id)
                prev_tool_end_count = previous_token_ids.count(
                    self.tool_call_end_token_id)
                cur_tool_start_count = current_token_ids.count(
                    self.tool_call_start_token_id)
                cur_tool_end_count = current_token_ids.count(
                    self.tool_call_end_token_id)

                # a cheap case - we're generating text, NOT tool calls.
                if cur_tool_start_count == cur_tool_end_count and prev_tool_end_count == cur_tool_end_count:
                    logger.debug(
                        'Generating text content! skipping tool parsing.')
                    return DeltaMessage(content=delta_text)

                # most of the time, we're going in here - we need to do partial JSON parsing and build stuff.
                else:
                    # flags for partial JSON parting. exported constants from "Allow" are handled via BIT MASK
                    # generally, we don't allow sending an incomplete function name. so we don't allow
                    flags = Allow.ALL if self.current_tool_name_sent else Allow.ALL & ~Allow.STR

                    # if a new tool call is being started. unusual since normally the first "cheap case" will be hit.
                    if cur_tool_start_count > cur_tool_end_count and cur_tool_start_count > prev_tool_start_count:
                        if len(delta_token_ids) > 1:
                            tool_call_portion = current_text.split(
                                self.tool_call_start_token)[-1]
                            text_portion = None
                        else:
                            tool_call_portion = None
                            text_portion = None
                            delta = None

                        # set cursors and state appropriately
                        self.current_tool_id += 1
                        self.current_tool_name_sent = False
                        self.current_tool_initial_sent = False
                        self.streamed_args_for_tool.append('')
                        logger.debug(
                            f'Starting on a new tool {self.current_tool_id}')

                    # if an existing tool call is being updated - the most common case!
                    elif cur_tool_start_count > cur_tool_end_count and cur_tool_start_count == prev_tool_start_count:
                        tool_call_portion = current_text.split(
                            self.tool_call_start_token)[-1]
                        text_portion = None

                    # if the current tool call is being closed
                    elif cur_tool_start_count == cur_tool_end_count and cur_tool_end_count > prev_tool_end_count:
                        logger.debug('Closing the current tool call!')
                        diff = self.prev_tool_call_arr[
                            self.current_tool_id].get('arguments')
                        if diff:
                            diff = json.dumps(diff).replace(
                                self.streamed_args_for_tool[
                                    self.current_tool_id], '')
                            logger.debug(
                                f'Finishing tool and found diff that wasn\'t streamed yet: {diff}'
                            )
                            return DeltaMessage(tool_calls=[
                                DeltaToolCall(index=self.current_tool_id,
                                              function=DeltaFunctionCall(
                                                  arguments=diff).model_dump(
                                                      exclude_none=True))
                            ])

                    else:
                        logger.error(
                            'INVARIANT - invalid state trying to parse tool calls (wtf?)'
                        )
                        delta = None
                        return delta

                    logger.debug(f'Tool call portion: {tool_call_portion}')
                    current_tool_call = partial_json_parser.loads(
                        tool_call_portion,
                        flags) if tool_call_portion else None
                    logger.debug(f'Parsed tool call {current_tool_call}')

                    # make sure to send the initial message first if we haven't already - with the tool ID
                    if not self.current_tool_initial_sent:
                        logger.debug('Sending InitialDeltaToolCall')
                        self.current_tool_initial_sent = True
                        return DeltaMessage(tool_calls=[
                            InitialDeltaToolCall(
                                index=self.current_tool_id).model_dump(
                                    exclude_none=True)
                        ])

                    # after that, make sure we send the function name before any arguments
                    elif not self.current_tool_name_sent:
                        function_name: Union[
                            str, None] = current_tool_call.get('name')
                        if function_name:
                            logger.debug(
                                f'Sending DeltaToolCall with function name {function_name}!'
                            )
                            self.current_tool_name_sent = True
                            return DeltaMessage(tool_calls=[
                                DeltaToolCall(index=self.current_tool_id,
                                              function=DeltaFunctionCall(
                                                  name=function_name).
                                              model_dump(exclude_none=True))
                            ])
                        else:
                            return None
                    else:
                        # if there is no tool calls
                        if tool_call_portion is None:
                            # if there's text but not tool calls, send that - otherwise None to skip chunk
                            delta = DeltaMessage(
                                content=delta_text
                            ) if text_portion is not None else None
                        # now, the nitty-gritty of tool calls
                        else:
                            # now we have the portion to parse as tool call.
                            if text_portion is not None:
                                logger.debug(
                                    f'Also, will send text portion {text_portion}'
                                )

                            logger.debug(
                                f'Trying to parse current tool call with ID {self.current_tool_id}'
                            )
                            if len(self.prev_tool_call_arr
                                   ) <= self.current_tool_id:
                                self.prev_tool_call_arr.append({})
                                logger.debug(
                                    'Pushed dummy value into tool call arr')
                            # main logic for tool parsing here
                            prev_arguments = self.prev_tool_call_arr[
                                self.current_tool_id].get('arguments')
                            cur_arguments = current_tool_call.get(
                                'arguments'
                            )  # arguments, if any, in current dict

                            logger.debug(
                                f'Diffing old arguments {prev_arguments} against new ones {cur_arguments}'
                            )
                            if not cur_arguments and not prev_arguments:
                                logger.debug(
                                    f'Skipping text {delta_text} - no arguments!'
                                )
                                delta = None
                            elif not cur_arguments and prev_arguments:
                                logger.error(
                                    'INVARIANT - impossible to have arguments reset mid-call'
                                )
                                delta = None
                            elif cur_arguments and not prev_arguments:
                                cur_arguments_json = json.dumps(cur_arguments)
                                logger.debug(
                                    f'Finding {delta_text} in {cur_arguments_json}'
                                )
                                arguments_delta = cur_arguments_json[:
                                                                     cur_arguments_json
                                                                     .index(
                                                                         delta_text
                                                                     ) +
                                                                     len(delta_text
                                                                         )]
                                logger.debug(
                                    f'First tokens in arguments received: {arguments_delta}'
                                )
                                delta = DeltaMessage(tool_calls=[
                                    DeltaToolCall(index=self.current_tool_id,
                                                  function=DeltaFunctionCall(
                                                      arguments=arguments_delta
                                                  ).model_dump(
                                                      exclude_none=True))
                                ])
                                self.streamed_args_for_tool[
                                    self.current_tool_id] += arguments_delta

                            elif cur_arguments and prev_arguments:
                                cur_args_json = json.dumps(cur_arguments)
                                prev_args_json = json.dumps(prev_arguments)
                                logger.debug(
                                    f"Searching for diff between \n{cur_args_json}\n{prev_args_json}"
                                )
                                argument_diff = extract_intermediate_diff(
                                    cur_args_json, prev_args_json)
                                logger.debug(
                                    f'Got argument diff: {argument_diff}')
                                delta = DeltaMessage(tool_calls=[
                                    DeltaToolCall(index=self.current_tool_id,
                                                  function=DeltaFunctionCall(
                                                      arguments=argument_diff).
                                                  model_dump(
                                                      exclude_none=True))
                                ])
                                self.streamed_args_for_tool[
                                    self.current_tool_id] += argument_diff
                            else:
                                delta = None

                            # handle saving the state for the current tool into the "prev" list for use in diffing for
                            # the next iteration
                            if self.current_tool_id == len(
                                    self.prev_tool_call_arr) - 1:
                                self.prev_tool_call_arr[
                                    self.current_tool_id] = current_tool_call
                            else:
                                self.prev_tool_call_arr.append(
                                    current_tool_call)

                            # TODO REPLACE ME WITH TOOL CALL
                            #delta = DeltaMessage(content=delta_text)
                        return delta

            except Exception as e:
                logger.error(
                    f'Error trying to handle streaming tool call: {e}')
                logger.debug(
                    'Skipping chunk as a result of tool streaming extraction error'
                )
                return None  # do not stream a delta. skip this token ID.