"""Model inference over any OpenAI-compatible endpoint (Modal or Baseten).

All model access goes through ``chat()``, which enforces timeouts, parses tool calls into plain
data, and logs token usage without ever logging keys or message content.
"""
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class BasetenError(Exception):
    """Base class for Baseten integration failures."""


class BasetenConfigError(BasetenError):
    """Missing credentials or model configuration."""


class BasetenAPIError(BasetenError):
    """The request failed, timed out, or returned something unusable."""


def build_client():
    """Construct the OpenAI-compatible client, or explain why we cannot."""
    from openai import OpenAI

    api_key = getattr(settings, 'INFERENCE_API_KEY', '') or getattr(
        settings, 'BASETEN_API_KEY', '')
    if not api_key:
        raise BasetenConfigError(
            "No inference key is set (MODAL_API_KEY or BASETEN_API_KEY). "
            "The AI assistant is unavailable until a key is configured; "
            "the manual escrow forms keep working."
        )
    return OpenAI(
        base_url=getattr(settings, 'INFERENCE_BASE_URL', '') or getattr(
            settings, 'BASETEN_BASE_URL', 'https://inference.baseten.co/v1'
        ),
        api_key=api_key,
    )


def chat(messages, tools=None, timeout=None):
    """One inference round.

    Returns ``{'text': str, 'tool_calls': [...], 'usage': {...}}`` where each
    tool call is ``{'id': ..., 'name': ..., 'arguments': {...}}``.
    Raises BasetenError on any failure; the agent loop decides whether the
    task can continue without the model.
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
    model = (getattr(settings, 'INFERENCE_MODEL', '')
             or getattr(settings, 'BASETEN_MODEL', ''))

    kwargs = {'model': model, 'messages': messages, 'timeout': timeout}
    if tools:
        kwargs['tools'] = tools
        kwargs['tool_choice'] = 'auto'

    try:
        response = client.chat.completions.create(**kwargs)
    except APITimeoutError as exc:
        raise BasetenAPIError(f"Baseten request timed out: {exc}") from exc
    except (APIConnectionError, APIStatusError) as exc:
        raise BasetenAPIError(f"Baseten request failed: {exc}") from exc
    except BasetenError:
        raise
    except Exception as exc:
        raise BasetenAPIError(f"Unexpected Baseten failure: {exc}") from exc

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError) as exc:
        raise BasetenAPIError("Baseten returned no choices") from exc

    tool_calls = []
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or '{}')
        except (ValueError, AttributeError) as exc:
            raise BasetenAPIError(
                f"Model returned unparseable arguments for {call.function.name}"
            ) from exc
        if not isinstance(arguments, dict):
            raise BasetenAPIError(
                f"Model returned non-object arguments for {call.function.name}"
            )
        tool_calls.append({
            'id': call.id,
            'name': call.function.name,
            'arguments': arguments,
        })

    usage = getattr(response, 'usage', None)
    logger.info(
        "Baseten call ok: model=%s prompt=%s completion=%s tool_calls=%d",
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
