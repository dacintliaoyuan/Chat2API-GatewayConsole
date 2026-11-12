# -*- coding: utf-8 -*-
"""Pure SSE aggregation for the non-streaming Chat endpoint (N1).

This module lives in ``shared`` because both gateway applications bundle the same
decoder; it imports only the standard library and owns no host state.

``Handler._chat`` can serve a ``stream=false`` client from an SSE upstream when
``NONSTREAM_UPSTREAM_AGGREGATE`` is enabled. This module is the whole decoder:
it owns NO socket, NO credential, NO usage ledger and NO retry policy. The handler
feeds it the lines produced by ``_UpstreamStream`` (or raw bytes) and asks for ONE
Chat-completion JSON body only after the stream proved it ended.

Contract pinned here:

* Only ``data:`` lines carry payload; blank lines end an event, ``:`` comments and
  every other SSE field are ignored. Multi-line data values are joined with
  ``"\\n"`` (the SSE rule) and CRLF is accepted.
* Byte input is buffered up to the next newline and decoded as one UTF-8 unit, so a
  multibyte character split across chunks is reassembled, never mangled.
* Deltas aggregate PER choice index: ``content``, the reasoning channels
  (``reasoning`` / ``reasoning_content``) kept separate from content, and
  ``tool_calls`` keyed by their own index (id/type/name retained, arguments
  concatenated across frames). Parallel tool calls never mix. The legacy
  single-call ``delta.function_call`` maps to one ``message.tool_calls`` entry
  (type ``function``, name and arguments concatenated); when the same choice also
  carries ``tool_calls``, those win and the legacy channel is not emitted twice.
* Fields the upstream may attach are preserved instead of dropped: the top-level
  ``system_fingerprint`` / ``service_tier``, the choice-level ``logprobs`` and the
  ``refusal`` / ``annotations`` / ``audio`` pieces, which are aggregated into
  the non-stream message (their standard location). An upstream may put the last
  three on the choice instead of the delta; both locations are accepted. A field
  the upstream never sent stays absent; nothing is fabricated.
* Success is returned only after BOTH the real ``[DONE]`` sentinel and a
  non-empty ``finish_reason`` were observed (Q12=A). A ``[DONE]`` without a
  reason, a reason followed by plain EOF, and a stream with neither raises
  ``upstream_missing_terminal``; a bad frame or a 200-body explicit error
  raises ``AggregateError`` too. A partial completion is never returned as a
  success, and neither credential is invented. This is deliberately stricter than
  the streaming path, which reports ``upstream_missing_finish_reason`` after it
  already committed a 200. ``length`` / ``tool_calls`` and every other upstream
  reason are kept verbatim; a protocol-level normal end does not claim the user's
  task completed.
* ``usage`` is the upstream's own tail block, merged per field when several blocks
  arrive; only a legal non-negative integer overwrites an already reported count,
  so a later ``null`` / invalid value never erases it. Nested detail blocks
  recurse with the same rule. When no usage block arrives it stays absent
  (``None``) instead of a fabricated zero or a complete cost. No ``n`` is added.

The state grows with the upstream output only: one list slot per non-empty piece
(content, reasoning, refusal, annotation), one choice state per observed choice
index, one tool-call state per call that carries a non-empty
id/type/name/arguments, and one pending byte/line buffer. Empty
content/reasoning/refusal pieces, empty annotation lists and empty tool-call
fields are dropped before they reach the lists, so a long run of empty deltas adds
no per-frame list slot or tool-call state. Audio data/transcript are merged into
one string per choice. There is no new truncation or output cap here.

``AnthropicMessageAggregator`` (N2b) shares the same SSE framing and the same
"one body only after the upstream itself terminated" rule, but its own terminal
rule is unchanged by the Chat Q12=A rule: ``message_stop`` alone completes an
Anthropic message (Anthropic has no ``[DONE]``), its content is an indexed
block list (text / tool_use / thinking, any other block type preserved verbatim),
and its usage arrives in two places: ``message_start`` carries the input side and
``message_delta`` the output side. Blocks aggregate by their own index;
``input_json_delta`` fragments join and parse into the tool_use ``input`` dict;
thinking stays a separate block and keeps its signature; text never absorbs
thinking. A message is returned only after ``message_stop``; a missing
``message_stop``, a bad frame, a broken connection or an explicit error is a
failure. A block still open at ``message_stop`` is a truncated message and fails
too. Usage stays unknown when neither event reports it, and a placeholder
``output_tokens`` from ``message_start`` is not taken as the final output count;
non-count metadata the upstream reports (``service_tier``, ``cache_creation``) is
preserved instead of dropped.
"""

import json
import time
from dataclasses import dataclass, field

# The upstream's explicit-error message is bounded like the stream path's own
# explicit-error detail (chat2api._UPSTREAM_EXPLICIT_ERROR_MESSAGE_MAX_CHARS);
# this bounds the diagnostic reason, never the completion output.
_EXPLICIT_ERROR_MESSAGE_MAX_CHARS = 200


class AggregateError(Exception):
    """One failed aggregate: the caller must not return a success body."""

    def __init__(self, reason, message=None):
        self.reason = reason
        self.message = message
        super().__init__(message or reason)


@dataclass
class _ToolCallState:
    id: str = ""
    type: str = ""
    name: str = ""
    arguments: list = field(default_factory=list)


@dataclass
class _ChoiceState:
    content: list = field(default_factory=list)
    reasoning: dict = field(
        default_factory=lambda: {"reasoning": [], "reasoning_content": []})
    tool_calls: dict = field(default_factory=dict)
    # Legacy single-call delta.function_call; only emitted when the choice
    # carried no indexed tool_calls.
    function_call: _ToolCallState | None = None
    refusal: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    audio: dict | None = None
    logprobs: dict | None = None
    finish_reason: str | None = None


def _reported_count(value):
    """Non-negative int the upstream reported; None for null/invalid values."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _merge_usage(base, update):
    """Merge cumulative usage blocks; only a legal reported count overwrites.

    A later ``null`` (or any non-integer / negative / boolean value) never
    erases a count an earlier snapshot already reported, and it never creates a
    field. Nested detail blocks recurse with the same rule, so a later snapshot
    that reports ``prompt_tokens_details.cached_tokens: null`` keeps the earlier
    ``cached_tokens``. An absent block stays absent (None); no zero, cost or
    token count is invented here.
    """
    if not isinstance(update, dict) or not update:
        return base
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in update.items():
        if isinstance(value, dict):
            nested = _merge_usage(merged.get(key), value)
            if nested is not None:
                merged[key] = nested
            continue
        count = _reported_count(value)
        if count is not None:
            merged[key] = count
    return merged or None


def _merge_aux_dict(base, update):
    """Merge an auxiliary choice/message dict without inventing fields.

    Nested dicts merge recursively, list fields (``logprobs.content`` /
    ``logprobs.refusal``) append their entries, and every other field keeps the
    latest non-null value. ``null`` never erases an observed value and an empty
    update never creates one.
    """
    if update is None:
        return base
    if not isinstance(update, dict):
        raise AggregateError("upstream_bad_frame")
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in update.items():
        if value is None:
            continue
        if isinstance(value, dict):
            nested = _merge_aux_dict(merged.get(key), value)
            if nested is not None:
                merged[key] = nested
        elif isinstance(value, list):
            if not value:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = list(value)
            elif isinstance(existing, list):
                merged[key] = existing + list(value)
            else:
                raise AggregateError("upstream_bad_frame")
        else:
            merged[key] = value
    return merged or None


def _merge_audio(base, update):
    """Merge streamed ``delta.audio`` pieces into one message audio object.

    ``data`` (base64 chunks) and ``transcript`` (incremental text) concatenate
    across frames; ``id`` keeps the first reported value and ``expires_at`` the
    latest reported integer. ``null`` never erases an observed value.
    """
    if update is None:
        return base
    if not isinstance(update, dict):
        raise AggregateError("upstream_bad_frame")
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in update.items():
        if value is None:
            continue
        if key in ("data", "transcript"):
            if not isinstance(value, str):
                raise AggregateError("upstream_bad_frame")
            if value:
                merged[key] = merged.get(key, "") + value
        elif key == "id":
            if not isinstance(value, str):
                raise AggregateError("upstream_bad_frame")
            if value and not merged.get("id"):
                merged["id"] = value
        elif key == "expires_at":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AggregateError("upstream_bad_frame")
            merged[key] = value
        else:
            merged[key] = value
    return merged or None


def _append_annotations(base, update):
    """Append incremental annotation entries; ``null``/empty adds nothing."""
    if update is None:
        return base
    if not isinstance(update, list):
        raise AggregateError("upstream_bad_frame")
    if not update:
        return base
    return (list(base) if isinstance(base, list) else []) + list(update)


def _explicit_error_reason(chunk):
    """Sanitized reason of a truthy top-level ``error`` object, else None."""
    error = chunk.get("error")
    if not error:
        return None
    message = error
    if isinstance(error, dict):
        message = error.get("message")
    if isinstance(message, str) and message.strip():
        text = message.replace("\r", " ").replace("\n", " ")
        return " ".join(text.split())[:_EXPLICIT_ERROR_MESSAGE_MAX_CHARS]
    return "upstream reported an explicit stream error"


class _SSEAggregator:
    """SSE framing shared by the protocol aggregators.

    Only the wire framing lives here: a blank line ends an event, ``data:`` lines
    are joined with ``"\\n"`` (the SSE rule), CRLF is accepted, and a multibyte
    UTF-8 character split across byte chunks is reassembled before decoding.
    Subclasses implement ``_consume_payload`` (one event's data), plus their own
    ``_require_terminal`` / ``_finish_aggregate`` terminal rule.
    """

    def __init__(self):
        self._pending_bytes = bytearray()
        self._event_data = []
        self._done = False

    @property
    def finished(self):
        """True once the protocol's terminal event has been observed.

        ``finish()`` still validates the protocol's own success rule (Chat needs
        both ``[DONE]`` and a non-empty ``finish_reason``), so callers may take
        the early-exit branch only when this is True.
        """
        return self._done

    # ---------- byte / line input ----------

    def feed_bytes(self, data):
        """Feed raw SSE bytes; incomplete UTF-8 is reassembled before decoding."""
        if not data:
            return
        self._pending_bytes.extend(data)
        while True:
            newline = self._pending_bytes.find(b"\n")
            if newline < 0:
                return
            raw = bytes(self._pending_bytes[:newline])
            del self._pending_bytes[:newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise AggregateError("upstream_bad_frame")
            self.feed_line(line)

    def feed_line(self, line):
        """Feed one SSE line (without its newline); a blank line ends an event."""
        if self._done:
            return
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            self._dispatch_event()
            return
        if line.startswith(":"):
            return
        if line.startswith("data:"):
            value = line[len("data:"):]
            if value.startswith(" "):
                value = value[1:]
            self._event_data.append(value)
            return
        # Every other SSE field (event:, id:, retry:) is not payload.

    def finish(self):
        """Close the stream at EOF and return the one protocol body."""
        if not self._done:
            if self._pending_bytes:
                raw = bytes(self._pending_bytes)
                del self._pending_bytes[:]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise AggregateError("upstream_bad_frame")
                self.feed_line(line)
            if self._event_data:
                self._dispatch_event()
        # The success rule is validated even when the protocol's terminal event
        # was already seen: Chat needs BOTH [DONE] and a non-empty
        # finish_reason, so a bare [DONE] must not bypass the check.
        self._require_terminal()
        return self._finish_aggregate()

    # ---------- SSE event -> chunk ----------

    def _dispatch_event(self):
        payload = "\n".join(self._event_data)
        self._event_data = []
        if payload:
            self._consume_payload(payload)

    def _consume_payload(self, payload):
        raise NotImplementedError

    def _require_terminal(self):
        raise NotImplementedError

    def _finish_aggregate(self):
        raise NotImplementedError


class ChatCompletionAggregator(_SSEAggregator):
    """Aggregate Chat-completion SSE deltas into one completion object."""

    def __init__(self, default_model=None):
        super().__init__()
        self._default_model = default_model
        self._choices = {}
        self._id = None
        self._created = None
        self._model = None
        self._system_fingerprint = None
        self._service_tier = None
        self._usage = None
        self._finish_reason_seen = False

    # ---------- SSE event -> chunk ----------

    def _consume_payload(self, payload):
        if payload.strip() == "[DONE]":
            self._done = True
            return
        try:
            chunk = json.loads(payload)
        except (TypeError, ValueError):
            raise AggregateError("upstream_bad_frame")
        if not isinstance(chunk, dict):
            raise AggregateError("upstream_bad_frame")
        error_reason = _explicit_error_reason(chunk)
        if error_reason is not None:
            raise AggregateError("upstream_explicit_error", error_reason)
        self._observe_chunk(chunk)

    def _require_terminal(self):
        # Q12=A: a successful non-stream Chat aggregate needs BOTH the real
        # ``[DONE]`` sentinel and a non-empty ``finish_reason``. A bare
        # ``[DONE]``, a reason followed by plain EOF, and a stream with
        # neither are all incomplete terminals, so this path fails instead of
        # returning ``finish_reason: null``. The streaming path reports its own
        # ``upstream_missing_finish_reason`` after it already committed a 200;
        # this path has not committed one. Neither credential is invented.
        if not (self._done and self._finish_reason_seen):
            raise AggregateError("upstream_missing_terminal")

    def _finish_aggregate(self):
        return self.result()

    def _observe_chunk(self, chunk):
        chunk_id = chunk.get("id")
        if isinstance(chunk_id, str) and chunk_id and self._id is None:
            self._id = chunk_id
        created = chunk.get("created")
        if (isinstance(created, int) and not isinstance(created, bool)
                and self._created is None):
            self._created = created
        model = chunk.get("model")
        if isinstance(model, str) and model and self._model is None:
            self._model = model
        # Stable response metadata some upstreams attach to every chunk; keep
        # the first reported value and never fabricate one.
        system_fingerprint = chunk.get("system_fingerprint")
        if (isinstance(system_fingerprint, str) and system_fingerprint
                and self._system_fingerprint is None):
            self._system_fingerprint = system_fingerprint
        service_tier = chunk.get("service_tier")
        if (isinstance(service_tier, str) and service_tier
                and self._service_tier is None):
            self._service_tier = service_tier
        choices = chunk.get("choices")
        if choices is None:
            choices = []
        if not isinstance(choices, list):
            raise AggregateError("upstream_bad_frame")
        for choice in choices:
            if not isinstance(choice, dict):
                raise AggregateError("upstream_bad_frame")
            index = choice.get("index")
            if index is None:
                # A single-choice frame may omit the index (historical
                # single-answer shape); several choices without indices
                # would silently collapse into one answer, so that is a
                # malformed frame instead.
                if len(choices) > 1:
                    raise AggregateError("upstream_bad_frame")
                index = 0
            if (isinstance(index, bool) or not isinstance(index, int)
                    or index < 0):
                raise AggregateError("upstream_bad_frame")
            state = self._choices.get(index)
            if state is None:
                state = _ChoiceState()
                self._choices[index] = state
            delta = choice.get("delta")
            if delta is not None:
                if not isinstance(delta, dict):
                    raise AggregateError("upstream_bad_frame")
                self._observe_delta(state, delta)
            # Choice-level logprobs are incremental per chunk; null on a role
            # or usage frame never erases what an earlier frame reported.
            logprobs = choice.get("logprobs")
            if logprobs is not None:
                state.logprobs = _merge_aux_dict(state.logprobs, logprobs)
            # Refusal/annotations/audio normally arrive in the delta, but an
            # upstream may also put them on the choice; both locations are
            # preserved into the non-stream choice/message fields instead of
            # being dropped.
            choice_refusal = choice.get("refusal")
            if isinstance(choice_refusal, str):
                if choice_refusal:
                    state.refusal.append(choice_refusal)
            elif choice_refusal is not None:
                raise AggregateError("upstream_bad_frame")
            choice_annotations = choice.get("annotations")
            if choice_annotations is not None:
                state.annotations = _append_annotations(
                    state.annotations, choice_annotations)
            choice_audio = choice.get("audio")
            if choice_audio is not None:
                state.audio = _merge_audio(state.audio, choice_audio)
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                if not isinstance(finish_reason, str):
                    raise AggregateError("upstream_bad_frame")
                state.finish_reason = finish_reason
                self._finish_reason_seen = True
        usage = chunk.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise AggregateError("upstream_bad_frame")
            self._usage = _merge_usage(self._usage, usage)

    def _observe_delta(self, state, delta):
        content = delta.get("content")
        if isinstance(content, str):
            if content:
                state.content.append(content)
        elif content is not None:
            raise AggregateError("upstream_bad_frame")
        for key in ("reasoning", "reasoning_content"):
            piece = delta.get(key)
            if isinstance(piece, str):
                if piece:
                    state.reasoning[key].append(piece)
            elif piece is not None:
                raise AggregateError("upstream_bad_frame")
        refusal = delta.get("refusal")
        if isinstance(refusal, str):
            if refusal:
                state.refusal.append(refusal)
        elif refusal is not None:
            raise AggregateError("upstream_bad_frame")
        annotations = delta.get("annotations")
        if annotations is not None:
            state.annotations = _append_annotations(
                state.annotations, annotations)
        audio = delta.get("audio")
        if audio is not None:
            state.audio = _merge_audio(state.audio, audio)
        delta_logprobs = delta.get("logprobs")
        if delta_logprobs is not None:
            state.logprobs = _merge_aux_dict(state.logprobs, delta_logprobs)
        # Legacy single-call channel (``delta.function_call``). It maps to the
        # same non-stream ``message.tool_calls`` shape as an indexed call; when
        # the choice also carries ``tool_calls``, those win and the legacy
        # channel is not emitted a second time.
        function_call = delta.get("function_call")
        if function_call is not None:
            if not isinstance(function_call, dict):
                raise AggregateError("upstream_bad_frame")
            legacy_name = function_call.get("name")
            if not isinstance(legacy_name, str) and legacy_name is not None:
                raise AggregateError("upstream_bad_frame")
            legacy_arguments = function_call.get("arguments")
            if (not isinstance(legacy_arguments, str)
                    and legacy_arguments is not None):
                raise AggregateError("upstream_bad_frame")
            if legacy_name or legacy_arguments:
                legacy = state.function_call
                if legacy is None:
                    legacy = _ToolCallState()
                    state.function_call = legacy
                if legacy_name and not legacy.name:
                    legacy.name = legacy_name
                if legacy_arguments:
                    legacy.arguments.append(legacy_arguments)
        tool_calls = delta.get("tool_calls")
        if tool_calls is None:
            return
        if not isinstance(tool_calls, list):
            raise AggregateError("upstream_bad_frame")
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                raise AggregateError("upstream_bad_frame")
            index = tool_call.get("index")
            if index is None:
                # The historical single-call delta omits the index; several
                # tool calls without indices would concatenate into one call,
                # so that is a malformed frame instead.
                if len(tool_calls) > 1:
                    raise AggregateError("upstream_bad_frame")
                index = 0
            if (isinstance(index, bool) or not isinstance(index, int)
                    or index < 0):
                raise AggregateError("upstream_bad_frame")
            # Validate every field before deciding whether the frame carries
            # output: a malformed type must fail even when its value is empty.
            call_id = tool_call.get("id")
            if not isinstance(call_id, str) and call_id is not None:
                raise AggregateError("upstream_bad_frame")
            call_type = tool_call.get("type")
            if not isinstance(call_type, str) and call_type is not None:
                raise AggregateError("upstream_bad_frame")
            function = tool_call.get("function")
            if function is not None and not isinstance(function, dict):
                raise AggregateError("upstream_bad_frame")
            name = None
            arguments = None
            if function is not None:
                name = function.get("name")
                if not isinstance(name, str) and name is not None:
                    raise AggregateError("upstream_bad_frame")
                arguments = function.get("arguments")
                if not isinstance(arguments, str) and arguments is not None:
                    raise AggregateError("upstream_bad_frame")
            # An empty delta carries no output: it must not create a tool-call
            # state or an arguments list slot. Only a non-empty
            # id/type/name/arguments starts (or extends) the call.
            if not (call_id or call_type or name or arguments):
                continue
            call = state.tool_calls.get(index)
            if call is None:
                call = _ToolCallState()
                state.tool_calls[index] = call
            if call_id and not call.id:
                call.id = call_id
            if call_type and not call.type:
                call.type = call_type
            if name and not call.name:
                call.name = name
            if arguments:
                call.arguments.append(arguments)

    # ---------- result ----------

    def usage(self):
        """The upstream usage tail, or None when it never reported one."""
        return self._usage

    def output_estimate(self):
        """Lower-bound output estimate in tokens from aggregated text."""
        chars = 0
        for state in self._choices.values():
            chars += sum(len(piece) for piece in state.content)
            chars += sum(len(piece) for pieces in state.reasoning.values()
                         for piece in pieces)
        return chars // 4

    def result(self):
        """The ONE Chat-completion body for the aggregated deltas."""
        choices = []
        for index in sorted(self._choices):
            state = self._choices[index]
            message = {"role": "assistant",
                       "content": "".join(state.content)}
            for key in ("reasoning", "reasoning_content"):
                if state.reasoning[key]:
                    message[key] = "".join(state.reasoning[key])
            if state.tool_calls:
                tool_calls = []
                for call_index in sorted(state.tool_calls):
                    call = state.tool_calls[call_index]
                    function = {"arguments": "".join(call.arguments)}
                    if call.name:
                        function["name"] = call.name
                    # ``index`` is the streaming correlation key only: the
                    # non-stream tool-call shape carries id/type/function.
                    item = {"function": function}
                    if call.id:
                        item["id"] = call.id
                    if call.type:
                        item["type"] = call.type
                    tool_calls.append(item)
                message["tool_calls"] = tool_calls
            elif state.function_call is not None:
                # The legacy single-call delta has no id; the non-stream shape
                # expects one, but inventing it would fabricate upstream data.
                call = state.function_call
                function = {"arguments": "".join(call.arguments)}
                if call.name:
                    function["name"] = call.name
                message["tool_calls"] = [{"type": "function",
                                         "function": function}]
            if state.refusal:
                message["refusal"] = "".join(state.refusal)
            if state.annotations:
                message["annotations"] = state.annotations
            if state.audio is not None:
                message["audio"] = state.audio
            choice = {
                "index": index,
                "message": message,
                "finish_reason": state.finish_reason,
            }
            if state.logprobs is not None:
                choice["logprobs"] = state.logprobs
            choices.append(choice)
        result = {
            "id": self._id or "",
            "object": "chat.completion",
            "model": self._model or self._default_model or "",
            "choices": choices,
        }
        # spec-shared-08: a field the upstream never sent stays absent. The
        # previous version filled ``created`` with ``int(time.time())``, i.e. it
        # wrote the HOST CLOCK into the protocol output; the shared aggregator
        # is a pure parser and must never fabricate a value.
        if self._created is not None:
            result["created"] = self._created
        if self._system_fingerprint is not None:
            result["system_fingerprint"] = self._system_fingerprint
        if self._service_tier is not None:
            result["service_tier"] = self._service_tier
        if self._usage is not None:
            result["usage"] = self._usage
        return result


# The input side of an Anthropic usage block; ``message_delta`` reports only the
# output side, so an input count there would duplicate the message_start snapshot.
_ANTHROPIC_INPUT_USAGE_KEYS = frozenset({
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"})


def _merge_usage_metadata(base, update, skip=()):
    """Merge cumulative Anthropic usage blocks without inventing fields.

    A legal non-negative integer count overwrites the earlier report; a later
    ``null`` or invalid count never erases it (the same rule as the Chat
    aggregator's ``_merge_usage``). Nested blocks recurse with the same rule.
    Every other reported value (for example ``service_tier`` or ``cache_creation``)
    keeps the latest non-null value. Keys in ``skip`` are ignored, which is how a
    stream's initial placeholder is kept out of the final block.
    """
    if not isinstance(update, dict) or not update:
        return base
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in update.items():
        if key in skip or value is None:
            continue
        if isinstance(value, dict):
            nested = _merge_usage_metadata(merged.get(key), value)
            if nested is not None:
                merged[key] = nested
            continue
        existing = merged.get(key)
        if isinstance(existing, int) and not isinstance(existing, bool):
            # A reported count is evidence: only another legal count replaces
            # it, never a string/boolean/bool sentinel.
            count = _reported_count(value)
            if count is not None:
                merged[key] = count
            continue
        merged[key] = value
    return merged or None


@dataclass
class _AnthropicBlockState:
    index: int
    kind: str
    block: dict
    text: list = field(default_factory=list)
    thinking: list = field(default_factory=list)
    signature: list = field(default_factory=list)
    partial_json: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    stopped: bool = False


def _block_index(event):
    """Validate the non-negative integer content-block index of one event."""
    index = event.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise AggregateError("upstream_bad_frame")
    return index


class AnthropicMessageAggregator(_SSEAggregator):
    """Aggregate Anthropic Messages SSE events into one message object.

    The event contract pinned here:

    * ``message_start`` carries the message envelope (id/model/role and the
      INPUT side of usage). Its content list must be empty and its
      ``output_tokens`` is the stream's initial placeholder, not the final
      output count, so that key is not merged from this event.
    * ``content_block_start`` opens one block at its own index; the block object
      is preserved. Text / thinking blocks accumulate their own channels;
      tool_use keeps id/name/input. Any other block type (for example
      ``redacted_thinking``) is preserved verbatim and must not carry deltas.
    * ``content_block_delta`` appends text_delta, thinking_delta,
      signature_delta, input_json_delta (tool_use) or citations_delta (text)
      fragments to the block at the SAME index. Thinking never becomes text and
      the signature is kept with its thinking block.
    * ``content_block_stop`` closes one block. ``message_delta`` carries the
      stop reason / stop sequence and the OUTPUT side of usage.
      ``message_stop`` is the ONLY success terminal; ``ping`` is ignored.
    * A message is returned only after ``message_stop``. A missing
      ``message_stop``, a bad frame, an unknown event type or delta type, an
      explicit error, or a block still open at ``message_stop`` is a failure.
      Usage stays absent (unknown) when no event reported it; nothing is
      fabricated.

    State grows with the upstream output only: one block state per content-block
    index, one list slot per non-empty text/thinking/signature/JSON fragment, one
    citation dict per reported citation, and one pending byte/line buffer. Empty
    deltas add no list slot. There is no new truncation or output cap here.
    """

    def __init__(self, default_model=None):
        super().__init__()
        self._default_model = default_model
        self._message = {}
        self._blocks = {}
        self._usage = None
        self._stop_reason = None
        self._stop_sequence = None
        self._saw_stop_sequence = False
        self._started = False

    # ---------- SSE event -> message ----------

    def _consume_payload(self, payload):
        if payload.strip() == "[DONE]":
            # Anthropic has no [DONE] sentinel: only message_stop terminates
            # this protocol, so a bare [DONE] is a malformed frame.
            raise AggregateError("upstream_bad_frame")
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            raise AggregateError("upstream_bad_frame")
        if not isinstance(event, dict):
            raise AggregateError("upstream_bad_frame")
        error_reason = _explicit_error_reason(event)
        if error_reason is not None:
            raise AggregateError("upstream_explicit_error", error_reason)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise AggregateError("upstream_bad_frame")
        if event_type == "message_start":
            self._observe_message_start(event)
        elif event_type == "content_block_start":
            self._observe_block_start(event)
        elif event_type == "content_block_delta":
            self._observe_block_delta(event)
        elif event_type == "content_block_stop":
            self._observe_block_stop(event)
        elif event_type == "message_delta":
            self._observe_message_delta(event)
        elif event_type == "message_stop":
            self._done = True
        elif event_type == "ping":
            return
        else:
            # An unknown event may carry output this aggregator cannot merge;
            # failing is safer than silently truncating the message.
            raise AggregateError("upstream_bad_frame")

    def _observe_message_start(self, event):
        if self._started:
            raise AggregateError("upstream_bad_frame")
        message = event.get("message")
        if not isinstance(message, dict):
            raise AggregateError("upstream_bad_frame")
        content = message.get("content")
        if content is not None and content != []:
            # The streaming contract starts with an empty content list; a
            # non-empty one would silently be dropped below.
            raise AggregateError("upstream_bad_frame")
        for key in ("id", "model", "role", "type"):
            value = message.get(key)
            if value is not None and not isinstance(value, str):
                raise AggregateError("upstream_bad_frame")
        if message.get("type") not in (None, "message"):
            raise AggregateError("upstream_bad_frame")
        usage = message.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise AggregateError("upstream_bad_frame")
            # message_start reports the INPUT side; its output_tokens is the
            # stream's initial placeholder, never the final output count.
            self._usage = _merge_usage_metadata(
                self._usage, usage, skip=("output_tokens",))
        self._message = {key: value for key, value in message.items()
                         if key not in ("content", "usage")}
        self._started = True

    def _observe_block_start(self, event):
        index = _block_index(event)
        if index in self._blocks:
            raise AggregateError("upstream_bad_frame")
        block = event.get("content_block")
        if not isinstance(block, dict):
            raise AggregateError("upstream_bad_frame")
        kind = block.get("type")
        if not isinstance(kind, str) or not kind:
            raise AggregateError("upstream_bad_frame")
        state = _AnthropicBlockState(index=index, kind=kind, block=dict(block))
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                if text:
                    state.text.append(text)
            elif text is not None:
                raise AggregateError("upstream_bad_frame")
        elif kind == "thinking":
            for name, pieces in (("thinking", state.thinking),
                                 ("signature", state.signature)):
                piece = block.get(name)
                if isinstance(piece, str):
                    if piece:
                        pieces.append(piece)
                elif piece is not None:
                    raise AggregateError("upstream_bad_frame")
        elif kind == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise AggregateError("upstream_bad_frame")
            if not isinstance(name, str) or not name:
                raise AggregateError("upstream_bad_frame")
            tool_input = block.get("input")
            if tool_input is not None and not isinstance(tool_input, dict):
                raise AggregateError("upstream_bad_frame")
        self._blocks[index] = state

    def _observe_block_delta(self, event):
        index = _block_index(event)
        state = self._blocks.get(index)
        if state is None or state.stopped:
            raise AggregateError("upstream_bad_frame")
        delta = event.get("delta")
        if not isinstance(delta, dict):
            raise AggregateError("upstream_bad_frame")
        delta_type = delta.get("type")
        if not isinstance(delta_type, str) or not delta_type:
            raise AggregateError("upstream_bad_frame")
        if delta_type == "text_delta" and state.kind == "text":
            piece = delta.get("text")
            if not isinstance(piece, str):
                raise AggregateError("upstream_bad_frame")
            if piece:
                state.text.append(piece)
        elif delta_type == "thinking_delta" and state.kind == "thinking":
            piece = delta.get("thinking")
            if not isinstance(piece, str):
                raise AggregateError("upstream_bad_frame")
            if piece:
                state.thinking.append(piece)
        elif delta_type == "signature_delta" and state.kind == "thinking":
            piece = delta.get("signature")
            if not isinstance(piece, str):
                raise AggregateError("upstream_bad_frame")
            if piece:
                state.signature.append(piece)
        elif delta_type == "input_json_delta" and state.kind == "tool_use":
            piece = delta.get("partial_json")
            if not isinstance(piece, str):
                raise AggregateError("upstream_bad_frame")
            if piece:
                state.partial_json.append(piece)
        elif delta_type == "citations_delta" and state.kind == "text":
            citation = delta.get("citation")
            if not isinstance(citation, dict):
                raise AggregateError("upstream_bad_frame")
            state.citations.append(citation)
        else:
            # A delta that does not belong to its block (or an unknown delta
            # type) would be dropped; fail instead of truncating.
            raise AggregateError("upstream_bad_frame")

    def _observe_block_stop(self, event):
        index = _block_index(event)
        state = self._blocks.get(index)
        if state is None or state.stopped:
            raise AggregateError("upstream_bad_frame")
        state.stopped = True

    def _observe_message_delta(self, event):
        delta = event.get("delta")
        if delta is not None:
            if not isinstance(delta, dict):
                raise AggregateError("upstream_bad_frame")
            stop_reason = delta.get("stop_reason")
            if stop_reason is not None:
                if not isinstance(stop_reason, str):
                    raise AggregateError("upstream_bad_frame")
                self._stop_reason = stop_reason
            stop_sequence = delta.get("stop_sequence")
            if stop_sequence is not None:
                if not isinstance(stop_sequence, str):
                    raise AggregateError("upstream_bad_frame")
                self._stop_sequence = stop_sequence
                self._saw_stop_sequence = True
        usage = event.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise AggregateError("upstream_bad_frame")
            # message_delta reports the OUTPUT side; an input count here would
            # duplicate the message_start snapshot.
            self._usage = _merge_usage_metadata(
                self._usage, usage, skip=_ANTHROPIC_INPUT_USAGE_KEYS)

    def _require_terminal(self):
        # Anthropic's own terminal rule is unchanged by the Chat Q12=A rule:
        # ``message_stop`` alone completes a message (there is no ``[DONE]``).
        if not self._done:
            raise AggregateError("upstream_missing_terminal")

    def _finish_aggregate(self):
        return self.result()

    # ---------- result ----------

    def usage(self):
        """The merged Anthropic usage block, or None when none was reported."""
        return self._usage

    def output_estimate(self):
        """Lower-bound output estimate in tokens from text and thinking."""
        chars = 0
        for state in self._blocks.values():
            chars += sum(len(piece) for piece in state.text)
            chars += sum(len(piece) for piece in state.thinking)
        return chars // 4

    def result(self):
        """The ONE Anthropic message body for the aggregated events."""
        content = []
        for index in sorted(self._blocks):
            state = self._blocks[index]
            if not state.stopped:
                # A message that stops with an open block is truncated: never
                # return the partial block as a complete message.
                raise AggregateError("upstream_bad_frame")
            content.append(self._block_result(state))
        message = dict(self._message)
        message["type"] = "message"
        message["role"] = "assistant"
        message["content"] = content
        message["model"] = message.get("model") or self._default_model or ""
        if "id" not in message:
            message["id"] = ""
        message.setdefault("stop_reason", None)
        if self._stop_reason is not None:
            message["stop_reason"] = self._stop_reason
        if self._saw_stop_sequence:
            message["stop_sequence"] = self._stop_sequence
        else:
            message.setdefault("stop_sequence", None)
        if self._usage is not None:
            message["usage"] = self._usage
        return message

    @staticmethod
    def _block_result(state):
        block = dict(state.block)
        if state.kind == "text":
            block["text"] = "".join(state.text)
            if state.citations:
                block["citations"] = state.citations
        elif state.kind == "thinking":
            block["thinking"] = "".join(state.thinking)
            if state.signature:
                block["signature"] = "".join(state.signature)
        elif state.kind == "tool_use":
            if state.partial_json:
                raw = "".join(state.partial_json)
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    raise AggregateError("upstream_bad_frame")
                if not isinstance(parsed, dict):
                    raise AggregateError("upstream_bad_frame")
                block["input"] = parsed
        return block
