"""Model inference over any OpenAI-compatible endpoint.

The chat model is just an HTTP API: point INFERENCE_BASE_URL at any
OpenAI-compatible server (Modal shared endpoint, self-hosted vLLM,
any other provider) and the agent loop works unchanged. Adding a new
AI server means setting three env vars — no code changes.
"""
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class InferenceError(Exception):
    """Base class for chat-model integration failures."""


class InferenceConfigError(InferenceError):
    """Missing credentials or model configuration."""


class InferenceAPIError(InferenceError):
    """The request failed, timed out, or returned something unusable."""


def build_client():
    """Construct the OpenAI-compatible client, or explain why we cannot."""
    from openai import OpenAI

    api_key = getattr(settings, 'INFERENCE_API_KEY', '')
    if not api_key:
        raise InferenceConfigError(
            "No inference key is set (INFERENCE_API_KEY). "
            "The AI assistant is unavailable until a key is configured; "
            "the manual escrow forms keep working."
        )
    return OpenAI(
        base_url=getattr(settings, 'INFERENCE_BASE_URL', ''),
        api_key=api_key,
    )


def chat(messages, tools=None, timeout=None, affinity_key=None):
    """One inference round.

    Returns ``{'text': str, 'tool_calls': [...], 'usage': {...}}`` where each
    tool call is ``{'id': ..., 'name': ..., 'arguments': {...}}``.
    ``affinity_key`` (e.g. a conversation id) is forwarded as
    ``Modal-Routing-Affinity-Key`` so multi-turn agent loops keep landing
    on the same warm container; other providers ignore the header.
    ``max_tokens`` comes from settings: reasoning models spend the budget
    on chain-of-thought first, and too small a budget returns empty
    content with ``finish_reason == 'length'``.
    Raises InferenceError on any failure; the agent loop decides
    whether the task can continue without the model.
    """
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
    )

    timeout = timeout if timeout is not None else getattr(
        settings, 'AGENT_REQUEST_TIMEOUT', 60
    )
    client = build_client()
    model = getattr(settings, 'INFERENCE_MODEL', '')
    max_tokens = int(getattr(settings, 'INFERENCE_MAX_TOKENS', 16384))

    kwargs = {'model': model, 'messages': messages, 'timeout': timeout,
              'max_tokens': max_tokens}
    if tools:
        kwargs['tools'] = tools
        kwargs['tool_choice'] = 'auto'
    if affinity_key:
        kwargs['extra_headers'] = {
            'Modal-Routing-Affinity-Key': str(affinity_key)}

    try:
        response = client.chat.completions.create(**kwargs)
    except APITimeoutError as exc:
        raise InferenceAPIError(f"Inference request timed out: {exc}") from exc
    except (APIConnectionError, APIStatusError) as exc:
        raise InferenceAPIError(f"Inference request failed: {exc}") from exc
    except InferenceError:
        raise
    except Exception as exc:
        raise InferenceAPIError(f"Unexpected inference failure: {exc}") from exc

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError) as exc:
        raise InferenceAPIError("Model returned no choices") from exc

    tool_calls = []
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or '{}')
        except (ValueError, AttributeError) as exc:
            raise InferenceAPIError(
                f"Model returned unparseable arguments for {call.function.name}"
            ) from exc
        if not isinstance(arguments, dict):
            raise InferenceAPIError(
                f"Model returned non-object arguments for {call.function.name}"
            )
        tool_calls.append({
            'id': call.id,
            'name': call.function.name,
            'arguments': arguments,
        })

    usage = getattr(response, 'usage', None)
    logger.info(
        "Inference call ok: model=%s prompt=%s completion=%s tool_calls=%d",
        model,
        getattr(usage, 'prompt_tokens', '?'),
        getattr(usage, 'completion_tokens', '?'),
        len(tool_calls),
    )
    return {
        'text': message.content or '',
        'tool_calls': tool_calls,
        'usage': {
            'prompt_tokens': getattr(usage, 'prompt_tokens', None),
            'completion_tokens': getattr(usage, 'completion_tokens', None),
        },
    }
