"""LLM access. The model is stateless: every call gets app-assembled context (spec Section 2).

Provider is chosen by model name: `claude-*` -> Anthropic, `gemini-*` -> Google. Tools are defined once
(Anthropic shape: name/description/input_schema) and converted for Gemini.

Per-call context goes in front of the latest user message, not in the system prompt, so the fixed
prefix (system + tools + earlier conversation) stays byte-identical and cacheable across turns.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as gt

from .config import ROOT, settings
from .db import db_session
from .models import KV, LlmUsage

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 12
FALLBACK_BETA = "server-side-fallback-2026-07-01"
USAGE_FILE = os.getenv("LLM_USAGE_FILE", str(ROOT / "data" / "llm_usage.jsonl"))

# $ per 1M tokens: (input, cached input, output). Estimates for comparing models - check the price pages.
PRICES = {
    "claude-opus-5": (5.0, 0.5, 25.0),
    "claude-sonnet-5": (2.0, 0.2, 10.0),
    "claude-haiku-4-5": (1.0, 0.1, 5.0),
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
    "gemini-3.1-pro": (2.0, 0.2, 12.0),
}
# Announced price changes: (effective from, model, prices). Google doubles 3.8 Flash on 2027-01-01.
PRICE_CHANGES = [
    (datetime(2027, 1, 1, tzinfo=timezone.utc), "gemini-3.8-flash", (1.5, 0.15, 7.5)),
]

# Models switchable from Telegram (/model): short name -> model id. Both keys go to the cloud host.
SWITCHABLE = {"gemini": "gemini-3.8-flash", "sonnet": "claude-sonnet-5"}
MODEL_KV_KEY = "llm_model"
_active_model: str | None = None  # cache of the /model choice (kv); one process, so no staleness

_anthropic: anthropic.AsyncAnthropic | None = None
_gemini: genai.Client | None = None


class LLMUnavailable(RuntimeError):
    """Carries a user-facing (Russian) explanation - spec Section 7 graceful failure."""


ToolExecutor = Callable[[str, dict], Awaitable[str]]


def price_for(model: str, at: datetime) -> tuple[float, float, float] | None:
    price = PRICES.get(model)
    for since, name, changed in PRICE_CHANGES:
        if name == model and at >= since:
            price = changed
    return price


def active_model() -> str:
    """The /model choice stored in the DB, else LLM_MODEL from the environment."""
    global _active_model
    if _active_model is None:
        with db_session() as db:
            row = db.get(KV, MODEL_KV_KEY)
            _active_model = (row.value or {}).get("model") if row else None
        _active_model = _active_model or settings.llm_model
    return _active_model


def set_active_model(db, model: str) -> None:
    """Writes through the caller's session (a second SQLite connection would wait on its transaction)."""
    global _active_model
    db.merge(KV(key=MODEL_KV_KEY, value={"model": model}))
    _active_model = model


def model_name(override: str | None = None) -> str:
    return override or active_model()


def _is_gemini(model: str) -> bool:
    return model.startswith("gemini")


def _record_usage(model: str, purpose: str, inp: int, cached: int, out: int, seconds: float) -> None:
    price = price_for(model, datetime.now(timezone.utc))
    cost = None
    if price:
        cost = ((inp - cached) * price[0] + cached * price[1] + out * price[2]) / 1_000_000
    row = {"ts": datetime.now(timezone.utc).isoformat(), "model": model, "purpose": purpose,
           "input": inp, "cached": cached, "output": out, "cost_usd": round(cost, 6) if cost is not None else None,
           "seconds": round(seconds, 2)}
    # Postgres only: a separate SQLite connection would wait on the handler's open write transaction.
    if not settings.database_url.startswith("sqlite"):
        try:
            with db_session() as db:
                db.add(LlmUsage(**{**row, "ts": datetime.fromisoformat(row["ts"])}))
        except Exception:
            log.warning("could not write usage row to the DB", exc_info=True)
    if not USAGE_FILE:  # cloud host: the DB row is the record
        return
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with open(USAGE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        log.warning("could not write usage log")


def with_context(messages: list[dict], context: str) -> list[dict]:
    """messages: [{"role": "user"|"assistant", "content": str}], last one is the new user message."""
    if not context:
        return messages
    *head, last = messages
    return [*head, {"role": "user", "content": f"<context>\n{context}\n</context>\n\n{last['content']}"}]


# ---------------------------------------------------------------- Anthropic

def _anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic
    import anthropic  # imported on first use: ~3 s of cold start that a Gemini-only instance never needs
    if _anthropic is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("Ключ ANTHROPIC_API_KEY не задан в .env.")
        _anthropic = anthropic.AsyncAnthropic(timeout=120.0, max_retries=2)
    return _anthropic


async def _anthropic_create(model: str, purpose: str, **kwargs):
    import anthropic
    started = time.monotonic()
    client = _anthropic_client()
    try:
        if model.startswith(("claude-opus", "claude-fable")):
            response = await client.beta.messages.create(model=model, betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            response = await client.messages.create(model=model, **kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable("Не могу достучаться до модели: ключ API недействителен. Проверь ANTHROPIC_API_KEY.") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMUnavailable("Модель недоступна: нет прав у ключа API или закончился баланс. Проверь аккаунт Anthropic.") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("Модель перегружена или превышен лимит запросов. Попробуй ещё раз через минуту.") from e
    except anthropic.BadRequestError as e:
        msg = str(getattr(e, "message", e))
        if any(w in msg.lower() for w in ("credit", "balance", "billing")):
            raise LLMUnavailable("Не могу достучаться до модели — проверь баланс API (console.anthropic.com → Billing).") from e
        log.exception("bad request to LLM")
        raise LLMUnavailable(f"Ошибка запроса к модели: {msg[:200]}") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"Сервис модели ответил ошибкой ({e.status_code}). Попробуй ещё раз чуть позже.") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable("Нет связи с моделью (сеть/таймаут). Попробуй ещё раз.") from e
    if response.stop_reason == "refusal":
        raise LLMUnavailable("Модель отказалась отвечать на этот запрос. Попробуй переформулировать.")
    u = response.usage
    cached = (u.cache_read_input_tokens or 0)
    total_in = u.input_tokens + cached + (u.cache_creation_input_tokens or 0)
    _record_usage(model, purpose, total_in, cached, u.output_tokens, time.monotonic() - started)
    return response


async def _anthropic_tools_loop(model, purpose, system, messages, tools, executor, effort, max_tokens) -> str:
    convo: list[dict] = [dict(m) for m in messages]
    texts: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        kwargs = dict(
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=convo,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        )
        if tools:
            kwargs["tools"] = tools
        response = await _anthropic_create(model, purpose, **kwargs)
        texts = [b.text for b in response.content if b.type == "text" and b.text.strip()]
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            return "\n\n".join(texts).strip()
        convo.append({"role": "assistant", "content": response.content})
        results = []
        for block in tool_uses:
            output, is_error = await _run_tool(executor, block.name, block.input)
            result = {"type": "tool_result", "tool_use_id": block.id, "content": output}
            if is_error:
                result["is_error"] = True
            results.append(result)
        convo.append({"role": "user", "content": results})
    return "\n\n".join(texts).strip()


async def _anthropic_structured(model, purpose, system, user, schema, effort, max_tokens) -> dict:
    response = await _anthropic_create(
        model, purpose, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    return json.loads(next((b.text for b in response.content if b.type == "text"), "{}"))


# ---------------------------------------------------------------- Gemini

GEMINI_LEVEL = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}


def _gemini_client() -> genai.Client:
    global _gemini
    if _gemini is None:
        if not os.getenv("GEMINI_API_KEY"):
            raise LLMUnavailable("Ключ GEMINI_API_KEY не задан в .env.")
        _gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini


def _gemini_schema(schema):
    """Gemini's JSON-schema subset: drop keys it may reject."""
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k not in ("additionalProperties", "strict")}
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


def _gemini_contents(messages: list[dict]) -> list[gt.Content]:
    contents: list[gt.Content] = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        if contents and contents[-1].role == role:  # merge consecutive same-role turns
            contents[-1].parts.append(gt.Part(text=m["content"]))
        else:
            contents.append(gt.Content(role=role, parts=[gt.Part(text=m["content"])]))
    return contents


async def _gemini_generate(model: str, purpose: str, contents, config: gt.GenerateContentConfig):
    started = time.monotonic()
    client = _gemini_client()
    try:
        response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
    except genai_errors.APIError as e:
        code = getattr(e, "code", None)
        if code == 402:
            raise LLMUnavailable("Gemini: закончился баланс (Prepay). Пополни кредиты в AI Studio → Billing.") from e
        if code in (401, 403):
            raise LLMUnavailable("Gemini: ключ API недействителен или нет доступа. Проверь GEMINI_API_KEY и биллинг в AI Studio.") from e
        if code == 429:
            raise LLMUnavailable("Gemini: превышен лимит/квота (или закончился баланс). Проверь биллинг в AI Studio.") from e
        if code and code >= 500:
            raise LLMUnavailable(f"Сервис Gemini ответил ошибкой ({code}). Попробуй ещё раз чуть позже.") from e
        log.exception("gemini request failed")
        raise LLMUnavailable(f"Ошибка запроса к Gemini: {str(getattr(e, 'message', e))[:200]}") from e
    except Exception as e:  # network errors surface as httpx/aiohttp exceptions
        log.exception("gemini connection failed")
        raise LLMUnavailable("Нет связи с Gemini (сеть/таймаут). Попробуй ещё раз.") from e
    u = response.usage_metadata
    if u:
        out = (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        _record_usage(model, purpose, u.prompt_token_count or 0, u.cached_content_token_count or 0, out,
                      time.monotonic() - started)
    if not response.candidates:
        raise LLMUnavailable("Gemini не вернул ответ (возможно, сработал фильтр). Попробуй переформулировать.")
    return response


def _gemini_config(system, effort, max_tokens, tools=None, schema=None) -> gt.GenerateContentConfig:
    kwargs = dict(
        system_instruction=system,
        max_output_tokens=max_tokens,
        thinking_config=gt.ThinkingConfig(thinking_level=GEMINI_LEVEL.get(effort, "MEDIUM")),
    )
    if tools:
        kwargs["tools"] = [gt.Tool(function_declarations=[
            gt.FunctionDeclaration(name=t["name"], description=t["description"],
                                   parameters_json_schema=_gemini_schema(t["input_schema"])) for t in tools])]
        kwargs["automatic_function_calling"] = gt.AutomaticFunctionCallingConfig(disable=True)
    if schema:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_json_schema"] = _gemini_schema(schema)
    return gt.GenerateContentConfig(**kwargs)


async def _gemini_tools_loop(model, purpose, system, messages, tools, executor, effort, max_tokens) -> str:
    contents = _gemini_contents(messages)
    config = _gemini_config(system, effort, max_tokens, tools=tools)
    text = ""
    for _ in range(MAX_TOOL_ROUNDS):
        response = await _gemini_generate(model, purpose, contents, config)
        content = response.candidates[0].content
        parts = (content.parts if content else None) or []
        text = "".join(p.text for p in parts if p.text and not p.thought).strip()
        calls = [p.function_call for p in parts if p.function_call]
        if not calls:
            return text
        contents.append(content)  # keep the model turn as-is (preserves thought signatures)
        replies = []
        for call in calls:
            output, is_error = await _run_tool(executor, call.name, dict(call.args or {}))
            payload = {"error": output} if is_error else {"result": output}
            replies.append(gt.Part(function_response=gt.FunctionResponse(id=call.id, name=call.name, response=payload)))
        contents.append(gt.Content(role="user", parts=replies))
    return text


async def _gemini_structured(model, purpose, system, user, schema, effort, max_tokens) -> dict:
    response = await _gemini_generate(model, purpose, [gt.Content(role="user", parts=[gt.Part(text=user)])],
                                      _gemini_config(system, effort, max_tokens, schema=schema))
    return json.loads(response.text or "{}")


# ---------------------------------------------------------------- public API

async def _run_tool(executor: ToolExecutor, name: str, args) -> tuple[str, bool]:
    try:
        return await executor(name, args if isinstance(args, dict) else {}), False
    except Exception as e:  # tool errors go back to the model, not to the user
        log.exception("tool %s failed", name)
        return f"Error: {e}", True


async def run_tools_loop(*, stable_system: str, context: str, messages: list[dict], tools: list[dict],
                         executor: ToolExecutor, effort: str | None = None, max_tokens: int = 16000,
                         purpose: str = "chat", model: str | None = None) -> str:
    """Manual agentic loop; returns the final text. Tool side effects happen in `executor`."""
    model = model_name(model)
    effort = effort or settings.llm_effort
    msgs = with_context(messages, context)
    if _is_gemini(model):
        return await _gemini_tools_loop(model, purpose, stable_system, msgs, tools, executor, effort, max_tokens)
    return await _anthropic_tools_loop(model, purpose, stable_system, msgs, tools, executor, effort, max_tokens)


async def structured(*, stable_system: str, user: str, schema: dict, context: str = "", effort: str | None = None,
                     max_tokens: int = 16000, purpose: str = "structured", model: str | None = None) -> dict:
    """One call constrained to a JSON schema (synthesis, classification, /final extraction)."""
    model = model_name(model)
    effort = effort or settings.llm_synthesis_effort
    if context:
        user = f"<context>\n{context}\n</context>\n\n{user}"
    if _is_gemini(model):
        return await _gemini_structured(model, purpose, stable_system, user, schema, effort, max_tokens)
    return await _anthropic_structured(model, purpose, stable_system, user, schema, effort, max_tokens)
