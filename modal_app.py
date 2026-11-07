"""Serve the chat model on Modal behind an OpenAI-compatible endpoint.

The Django app talks to ANY OpenAI-compatible `/v1` endpoint (see
``agent/client.py``), so pointing it at Modal is a config change, not a
code change. This file is the Modal side: vLLM in-process, FastAPI
shim exposing ``POST /v1/chat/completions`` (plus ``/v1/models`` and
``/healthz``), Bearer-token auth, scale-to-zero when idle.

Prerequisites (Modal account required):
  pip install modal
  modal setup                      # paste MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
  modal secret create locktrans-llm \
    MODAL_AUTH_TOKEN=<pick-a-long-random-string> \
    MODEL_ID=meta-models/Muse-Spark-Open-816B-A42B-NVFP4 \
    HF_TOKEN=<only if the model is gated/private> \
    TOOL_CALL_PARSER=<see below>

Deploy:
  modal deploy modal_app.py        # note the https://.../v1 URL it prints

Wire Django (local .env or Render env vars):
  MODAL_BASE_URL=https://<your-modal-app>-v1.modal.run
  MODAL_API_KEY=<same MODAL_AUTH_TOKEN as above>
  MODAL_MODEL=<same MODEL_ID, echoed back; any string works>

Tool calling: vLLM serves the ``tools`` parameter natively when the
model's tokenizer ships a tool-capable chat template. Muse-Spark is a
custom architecture: if its template lacks tool support or vLLM cannot
load it, the agent loop still runs (plain replies, manual forms as
fallback) — verify with a live turn before judging. TOOL_CALL_PARSER
selects vLLM's parser (e.g. ``llama3_json`` for Llama-3.x, ``hermes``
for Qwen/Hermes-style, ``mistral`` for Mistral). Leave it empty to let
the model answer without structured tool calls and the agent loop
will degrade to plain replies for those turns.
"""

import os

import modal

# Public weights (no HF account needed unless gated):
#   meta-models/Muse-Spark-Open-816B-A42B-NVFP4   <- default, ~4-bit, smallest
#   meta-models/Muse-Spark-Open-816B-A42B         <- full precision, ~1.6TB
# 816B MoE (42B active) needs serious VRAM even quantized: the NVFP4
# build wants roughly 8xH100-80GB. That is $$$/hr on any cloud, so only
# deploy this for short judging/demo windows and let it scale to zero.
# For day-to-day dev, leave MODAL_* unset and use Baseten.
MODEL_ID = os.environ.get(
    "MODEL_ID", "meta-models/Muse-Spark-Open-816B-A42B-NVFP4")
TOOL_CALL_PARSER = os.environ.get("TOOL_CALL_PARSER", "") or None
GPU_TYPE = os.environ.get("MODAL_GPU", "H100")
GPU_COUNT = int(os.environ.get("MODAL_GPU_COUNT", "8"))
HF_TOKEN = os.environ.get("HF_TOKEN", "") or None

app = modal.App("locktrans-llm")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "vllm==0.8.5",
        "fastapi==0.115.12",
        "huggingface_hub==0.33.1",
    )
    .env({"HF_HUB_OFFLINE": "0"})
)

# Persistent HF cache so redeploys / cold starts don't re-download
# hundreds of GB of weights every time.
weights_cache = modal.Volume.from_name("locktrans-hf-cache", create_if_missing=True)


def _check_auth(request) -> bool:
    import secrets as _secrets

    expected = os.environ.get("MODAL_AUTH_TOKEN", "")
    if not expected:
        return False
    got = request.headers.get("authorization", "")
    if not got.lower().startswith("bearer "):
        return False
    return _secrets.compare_digest(got[7:], expected)


@app.cls(
    image=image,
    gpu=f"{GPU_TYPE}:{GPU_COUNT}" if GPU_COUNT > 1 else GPU_TYPE,
    volumes={"/root/.cache/huggingface": weights_cache},
    secrets=[modal.Secret.from_name("locktrans-llm")],
    scaledown_window=300,
    timeout=3600,
)
class Model:
    @modal.enter()
    def load(self):
        from vllm import AsyncLLMEngine
        from vllm.engine.arg_utils import AsyncEngineArgs

        # Custom Meta architecture: run the repo's own modeling code.
        # (Sandboxed in our own container; never enable this for
        # untrusted third-party repos.)
        kwargs = dict(
            model=MODEL_ID,
            trust_remote_code=True,
            max_model_len=8192,
            gpu_memory_utilization=0.9,
            enable_auto_tool_choice=True,
        )
        if TOOL_CALL_PARSER:
            kwargs["tool_call_parser"] = TOOL_CALL_PARSER
        if HF_TOKEN:
            kwargs["hf_overrides"] = {}
        engine_args = AsyncEngineArgs(**kwargs)
        try:
            from huggingface_hub import login

            if HF_TOKEN:
                login(token=HF_TOKEN)
        except Exception:
            pass
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        from vllm.transformers_utils.tokenizer import get_tokenizer

        self.tokenizer = get_tokenizer(MODEL_ID, trust_remote_code=True)

    async def _chat(self, body: dict) -> dict:
        import time
        import uuid
        from vllm import SamplingParams
        from vllm.entrypoints.openai.protocol import (
            ChatCompletionRequest,
            ChatCompletionResponse,
            ChatCompletionResponseChoice,
            ChatMessage,
            FunctionCall,
            ToolCall,
            UsageInfo,
        )

        req = ChatCompletionRequest(**body)
        request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        sampling = SamplingParams(
            temperature=req.temperature if req.temperature is not None else 0.7,
            top_p=req.top_p if req.top_p is not None else 1.0,
            max_tokens=req.max_tokens or 1024,
        )
        prompt = self.tokenizer.apply_chat_template(
            [m.model_dump() for m in req.messages],
            tokenize=False,
            add_generation_prompt=True,
            tools=[t.model_dump() for t in req.tools] if req.tools else None,
        )
        tool_calls_out = []
        content_parts = []
        async for output in self.engine.generate(
            prompt, sampling, request_id,
            tools=[t.model_dump() for t in req.tools] if req.tools else None,
        ):
            text = output.outputs[0].text or ""
            content_parts.append(text)
            for tc in output.outputs[0].tool_calls or []:
                tool_calls_out.append(
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:12]}",
                        type="function",
                        function=FunctionCall(
                            name=tc.function.name,
                            arguments=tc.function.arguments or "{}",
                        ),
                    )
                )
        message = ChatMessage(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls_out or None,
        )
        usage = UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        try:
            usage = UsageInfo(
                prompt_tokens=len((await self.engine.get_tokenizer()).encode(prompt)),
                completion_tokens=sum(
                    len(o.token_ids) for o in output.outputs
                ),
                total_tokens=0,
            )
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        except Exception:
            pass
        resp = ChatCompletionResponse(
            id=request_id,
            created=created,
            model=req.model,
            choices=[
                ChatCompletionResponseChoice(
                    index=0, message=message, finish_reason="stop"
                )
            ],
            usage=usage,
        )
        return resp.model_dump(exclude_none=True)

    @modal.method()
    async def chat_blocking(self, body: dict) -> dict:
        # Plain remote-callable entry point (useful for jobs/scripts).
        return await self._chat(body)

    @modal.web_endpoint(method="POST", docs=True)
    async def chat_completions(self, request):
        from fastapi import Response
        import json as _json

        if not _check_auth(request):
            return Response(
                content=_json.dumps({"error": "unauthorized"}),
                status_code=401,
                media_type="application/json",
            )
        body = await request.json()
        try:
            # Web endpoints on a Cls run inside the class container,
            # so the loaded engine is directly available on self.
            result = await self._chat(body)
        except Exception as exc:
            return Response(
                content=_json.dumps({"error": f"inference failed: {exc}"}),
                status_code=502,
                media_type="application/json",
            )
        return Response(content=_json.dumps(result), media_type="application/json")

    @modal.web_endpoint(method="GET", docs=True)
    async def models(self, request):
        from fastapi import Response
        import json as _json
        import time as _time

        return Response(
            content=_json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "created": int(_time.time()),
                            "owned_by": "modal",
                        }
                    ],
                }
            ),
            media_type="application/json",
        )

    @modal.web_endpoint(method="GET", docs=True)
    async def healthz(self, request):
        from fastapi import Response
        import json as _json

        return Response(
            content=_json.dumps({"ok": True, "model": MODEL_ID}),
            media_type="application/json",
        )
