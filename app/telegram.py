"""Minimal Telegram Bot API client - only the calls this bot needs."""

import html
import json
import re

import httpx

from .config import settings

API = f"https://api.telegram.org/bot{settings.telegram_token}"
MAX_LEN = 4000  # Telegram hard limit is 4096; leave room for HTML tags

_client = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=10.0))


class TelegramError(RuntimeError):
    pass


async def call(method: str, **params) -> dict:
    payload = {k: v for k, v in params.items() if v is not None}
    resp = await _client.post(f"{API}/{method}", json=payload)
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data["result"]


def to_html(text: str) -> str:
    """Model output uses **bold** / *italic* markdown; Telegram gets safe HTML."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.M)
    return out


def _chunks(text: str) -> list[str]:
    chunks, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > MAX_LEN and current:
            chunks.append(current)
            current = ""
        while len(para) > MAX_LEN:
            chunks.append(para[:MAX_LEN])
            para = para[MAX_LEN:]
        current = f"{current}\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks or [""]


async def send_message(chat_id: int, text: str, keyboard: list[list[dict]] | None = None) -> dict:
    """Sends (splitting long text); the keyboard goes on the last chunk. Returns the last message."""
    parts = _chunks(text)
    result = {}
    for i, part in enumerate(parts):
        markup = {"inline_keyboard": keyboard} if keyboard and i == len(parts) - 1 else None
        try:
            result = await call("sendMessage", chat_id=chat_id, text=to_html(part),
                                parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            # Malformed HTML from an odd model output - fall back to plain text.
            result = await call("sendMessage", chat_id=chat_id, text=part, reply_markup=markup)
    return result


async def edit_keyboard(chat_id: int, message_id: int, keyboard: list[list[dict]] | None) -> None:
    try:
        await call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": keyboard or []})
    except TelegramError:
        pass  # message too old / unchanged - harmless


async def answer_callback(callback_id: str, text: str | None = None) -> None:
    try:
        await call("answerCallbackQuery", callback_query_id=callback_id, text=text)
    except TelegramError:
        pass


async def send_typing(chat_id: int) -> None:
    try:
        await call("sendChatAction", chat_id=chat_id, action="typing")
    except (TelegramError, httpx.HTTPError):
        pass


async def send_document(chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
    resp = await _client.post(
        f"{API}/sendDocument",
        data={"chat_id": str(chat_id), "caption": caption},
        files={"document": (filename, content, "application/json")},
    )
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"sendDocument: {data.get('description')}")


async def get_updates(offset: int | None, timeout: int = 50) -> list[dict]:
    return await call("getUpdates", offset=offset, timeout=timeout,
                      allowed_updates=["message", "callback_query"])


async def set_webhook(url: str, secret: str) -> dict:
    return await call("setWebhook", url=url, secret_token=secret,
                      allowed_updates=["message", "callback_query"], drop_pending_updates=False)


async def delete_webhook() -> dict:
    return await call("deleteWebhook")


async def webhook_info() -> dict:
    return await call("getWebhookInfo")


def button(text: str, data: dict | str) -> dict:
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    return {"text": text, "callback_data": payload[:64]}
