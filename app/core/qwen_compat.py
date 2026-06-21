"""Qwen-tolerant OpenAI chat model.

The Qwen models we run via the OpenAI-compatible provider sometimes emit a
structured *tool-call argument* that should be a list/object as a JSON-ENCODED
STRING — e.g. a tool `submit(produced: list[str], notes: str)` is called with

    {"produced": "[\"outputs/t1.md\"]", "notes": "..."}

instead of a real array. pydantic-ai validates tool-call arguments against the
tool schema; a `str` where a `list[str]` is expected fails validation,
pydantic-ai re-prompts, Qwen repeats the mistake, and once the retry budget is
exhausted the agent run dies with `UnexpectedModelBehavior`.

The fix decodes such stringified collections back into real structures BEFORE
pydantic-ai validates them. Crucially it is **schema-aware**: it only decodes a
value when that tool parameter is actually typed as an array/object. This
matters because the inverse case is just as real — `write_file(content: str)`
legitimately receives a JSON-array *string* as its `content` (the agent is
writing a `.json` ops file), and that must be left as a string. A blind decoder
would turn that into a list and break the `str`-typed field. So we look up each
parameter's declared type from the tool's JSON schema and only unstringify the
ones that expect a collection.

`browser_agent.QwenToolCallChatOpenAI` handles the analogous problem for its
structured *output* path; this model covers plain tool calls (and, since these
agents use tool-output mode, structured output too — it travels as a
final-result tool call).
"""
from __future__ import annotations

import copy
import json
from typing import Any

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.settings import ModelSettings


def _resolve_ref(ref: str, defs: dict[str, Any]) -> Any:
    """Resolve a local JSON-Schema `$ref` (e.g. "#/$defs/TaskSpec") against the
    collected definitions. Only local refs into $defs/definitions are supported;
    anything else is returned as an empty schema (the model will treat it as
    unconstrained rather than choke on a dangling ref)."""
    parts = ref.lstrip("#/").split("/")
    if len(parts) == 2 and parts[0] in ("$defs", "definitions"):
        return defs.get(parts[1], {})
    return {}


def _inline_refs(node: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """Recursively replace every local `$ref` in `node` with a deep copy of the
    schema it points at, so the resulting JSON Schema is fully self-contained
    with no `$ref`/`$defs`/`definitions`. GLM-5.1 (and some other OpenAI-compat
    backends) reject schemas that use these; pydantic emits them for any nested
    model. `seen` guards against infinite recursion on a self-referential model
    by leaving a cycle's repeat occurrence as an empty (unconstrained) schema."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                # Recursive model: stop expanding; emit an unconstrained object.
                return {"type": "object"}
            resolved = _resolve_ref(ref, defs)
            inlined = _inline_refs(copy.deepcopy(resolved), defs, seen + (ref,))
            # Merge any sibling keys (e.g. "description") that sat alongside $ref.
            if isinstance(inlined, dict):
                for k, v in node.items():
                    if k != "$ref":
                        inlined.setdefault(k, v)
            return inlined
        return {
            k: _inline_refs(v, defs, seen)
            for k, v in node.items()
            if k not in ("$defs", "definitions")
        }
    if isinstance(node, list):
        return [_inline_refs(item, defs, seen) for item in node]
    return node


def _dereference_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `schema` with all local `$ref`s inlined and the
    `$defs`/`definitions` blocks stripped. No-op (returns the same object) when
    the schema has no refs/defs, so well-formed schemas are untouched."""
    if not isinstance(schema, dict):
        return schema
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    if not defs and "$ref" not in json.dumps(schema):
        return schema
    return _inline_refs(schema, defs)


def _collection_params(json_schema: dict[str, Any]) -> set[str]:
    """Return the names of top-level parameters in a tool's JSON schema whose
    type is array or object (i.e. the ones a stringified value should be
    decoded into). Handles `anyOf`/`oneOf` unions (e.g. `list[str] | None`)."""
    out: set[str] = set()
    props = (json_schema or {}).get("properties", {})
    for name, sch in props.items():
        types: set[str] = set()
        if "type" in sch:
            types.add(sch["type"])
        for branch in (*sch.get("anyOf", []), *sch.get("oneOf", [])):
            if isinstance(branch, dict) and "type" in branch:
                types.add(branch["type"])
        if types & {"array", "object"}:
            out.add(name)
    return out


def _decode_if_json_collection(value: Any) -> Any:
    """If `value` is a string that JSON-decodes to a list/dict, return the
    decoded structure; otherwise return it unchanged."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in ("[", "{"):
        return value
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return value
    return decoded if isinstance(decoded, (list, dict)) else value


def _repair_args(
    args: str | dict[str, Any] | None,
    collection_params: set[str],
) -> str | dict[str, Any] | None:
    """Repair a tool call's `args`: ensure it's a dict, then unstringify ONLY
    the values whose parameter is declared as a collection. Returns a dict
    (pydantic-ai accepts a dict or a JSON string for ToolCallPart.args)."""
    if args is None:
        return None
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return args
        try:
            args = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return args  # not JSON we understand; let pydantic-ai surface it
    if not isinstance(args, dict):
        return args
    return {
        k: (_decode_if_json_collection(v) if k in collection_params else v)
        for k, v in args.items()
    }


def _build_collection_param_map(
    params: ModelRequestParameters,
) -> dict[str, set[str]]:
    """Map each tool name -> set of its collection-typed parameter names, across
    both the agent's function tools and the output (final_result) tools."""
    mapping: dict[str, set[str]] = {}
    for tool in (*params.function_tools, *params.output_tools):
        mapping[tool.name] = _collection_params(tool.parameters_json_schema)
    return mapping


class QwenChatModel(OpenAIChatModel):
    """OpenAIChatModel that repairs Qwen's stringified collection tool-call args
    before pydantic-ai validates them — schema-aware, so only collection-typed
    parameters are decoded. Drop-in; behaviour is identical for well-formed
    responses."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.profile = OpenAIModelProfile(
            openai_supports_tool_choice_required=False
        ).update(self.profile)

    def _repair_response(
        self, response: ModelResponse, params: ModelRequestParameters
    ) -> ModelResponse:
        collection_map = _build_collection_param_map(params)
        for part in response.parts:
            if isinstance(part, ToolCallPart):
                part.args = _repair_args(
                    part.args, collection_map.get(part.tool_name, set())
                )
        return response

    def _inline_tool_schemas(
        self, params: ModelRequestParameters
    ) -> ModelRequestParameters:
        """Inline every `$ref`/`$defs` in the function- and output-tool schemas
        before they hit the wire. GLM-5.1 rejects non-inlined JSON Schema
        ($ref/$defs/definitions unsupported); pydantic emits refs for any nested
        model (PlanOutput → TaskSpec, etc.). We mutate the tool definitions in
        place since each is rebuilt per request from the agent's tool set."""
        for tool in (*params.function_tools, *params.output_tools):
            tool.parameters_json_schema = _dereference_schema(
                tool.parameters_json_schema
            )
        return params

    async def request(
        self,
        messages: Any,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        model_request_parameters = self._inline_tool_schemas(
            model_request_parameters
        )
        response = await super().request(
            messages, model_settings, model_request_parameters
        )
        return self._repair_response(response, model_request_parameters)
