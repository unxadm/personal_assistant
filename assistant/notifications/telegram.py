"""Telegram: a notification channel and a chat interface, via the Bot API.

Uses long polling, so the server does not need to be reachable from the internet. Only
the configured chat id is served; everyone else is ignored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..core.config import AppConfig, TelegramConfig
from ..plugins.sdk import Notification
from .service import Channel, ChannelError, absolute_link

log = logging.getLogger(__name__)

MAX_TEXT = 4000
POLL_SECONDS = 25


class TelegramChannel(Channel):
    name = "telegram"
    label = "Telegram"

    def __init__(self, http: httpx.AsyncClient, get_config: Callable[[], AppConfig]):
        self._http = http
        self._get_config = get_config
        # Wired up by the app: chat text -> reply, and (ref, action) -> confirmation.
        self.on_message: Callable[[str, list[tuple[bytes, str]]], Awaitable[str]] | None = None
        # Voice note bytes -> transcript (raises ValueError with a message for the user).
        self.on_voice: Callable[[bytes], Awaitable[str]] | None = None
        self.on_action: Callable[[str, str], Awaitable[str]] | None = None
        self._offset = 0
        self._task: asyncio.Task[None] | None = None

    def _cfg(self) -> TelegramConfig:
        return self._get_config().notifications.telegram

    async def _call(self, method: str, payload: dict[str, Any], timeout: float = 20.0) -> Any:
        cfg = self._cfg()
        if not cfg.bot_token:
            raise ChannelError("no bot token")
        url = f"{cfg.api_base.rstrip('/')}/bot{cfg.bot_token}/{method}"
        try:
            response = await self._http.post(url, json=payload, timeout=timeout)
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            # Never include the URL: it contains the bot token.
            raise ChannelError(f"Telegram {method}: {type(e).__name__}") from None
        if not data.get("ok"):
            raise ChannelError(f"Telegram {method}: {data.get('description', response.status_code)}")
        return data.get("result")

    # -- channel ----------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.enabled:
            return {"ready": False, "detail": "turned off"}
        if not cfg.bot_token:
            return {"ready": False, "detail": "missing: bot token"}
        if not cfg.chat_id:
            return {"ready": False, "detail": "missing: chat id — send /start to your bot to get it"}
        return {"ready": True, "detail": f"chat {cfg.chat_id}"}

    async def send(self, notification: Notification) -> None:
        text = notification.title
        if notification.body:
            text += "\n\n" + notification.body
        link = absolute_link(notification.url, self._get_config().server.public_url)
        if link:
            text += "\n" + link
        payload: dict[str, Any] = {"chat_id": self._cfg().chat_id, "text": text[:MAX_TEXT]}
        if notification.ref and notification.actions:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": a["label"], "callback_data": f"{a['id']}|{notification.ref}"[:64]}
                        for a in notification.actions
                    ]
                ]
            }
        await self._call("sendMessage", payload)

    async def _download(self, file_id: str) -> bytes:
        cfg = self._cfg()
        info = await self._call("getFile", {"file_id": file_id})
        limit = self._get_config().uploads.max_audio_mb * 1024 * 1024
        if int(info.get("file_size") or 0) > limit or not info.get("file_path"):
            raise ChannelError("that file is too large")
        url = f"{cfg.api_base.rstrip('/')}/file/bot{cfg.bot_token}/{info['file_path']}"
        try:
            response = await self._http.get(url, timeout=60.0)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise ChannelError(f"Telegram download: {type(e).__name__}") from None
        return response.content

    async def _say(self, chat_id: Any, text: str) -> None:
        for start in range(0, max(len(text), 1), MAX_TEXT):
            await self._call("sendMessage", {"chat_id": chat_id, "text": text[start : start + MAX_TEXT] or "…"})

    # -- incoming ---------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        cfg = self._cfg()
        if callback := update.get("callback_query"):
            chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
            if not cfg.chat_id or chat_id != cfg.chat_id:
                return
            action, _, ref = str(callback.get("data") or "").partition("|")
            answer = await self.on_action(ref, action) if self.on_action and ref else "Unknown action."
            await self._call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": answer[:200]})
            message = callback.get("message") or {}
            if message.get("message_id"):
                try:  # remove the buttons so they cannot be pressed twice
                    await self._call(
                        "editMessageReplyMarkup",
                        {"chat_id": chat_id, "message_id": message["message_id"],
                         "reply_markup": {"inline_keyboard": []}},
                    )
                except ChannelError:
                    pass
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (message.get("text") or "").strip()
        if not chat_id or chat.get("type") != "private":
            return
        if not cfg.chat_id:
            # Not linked yet: tell the sender their own id so the owner can enter it.
            if text.startswith("/start"):
                await self._say(
                    chat_id,
                    f"This assistant is not linked to a chat yet. Your chat id is {chat_id}. "
                    "Enter it under Settings → Notifications → Telegram.",
                )
            return
        if chat_id != cfg.chat_id:
            return  # strangers get no response at all
        voice = message.get("voice") or message.get("audio")
        document = message.get("document") or {}
        photo = (message.get("photo") or [None])[-1]  # sizes are listed smallest first
        if photo is None and str(document.get("mime_type", "")).startswith("image/"):
            photo = document
        if text.startswith("/start") and not (voice or photo):
            await self._say(chat_id, "Connected. Send a message, voice note or photo, or /new to start a fresh conversation.")
            return
        if not cfg.chat or self.on_message is None:
            await self._say(chat_id, "Chat over Telegram is turned off; I only send notifications here.")
            return

        heard = ""
        images: list[tuple[bytes, str]] = []
        try:
            if voice:
                if self.on_voice is None:
                    raise ValueError("Voice notes are not available.")
                text = await self.on_voice(await self._download(voice["file_id"]))
                if not text:
                    raise ValueError("I couldn't make out any words in that.")
                heard = f"🎤 “{text}”\n\n"  # show what was understood
            elif photo:
                images = [(await self._download(photo["file_id"]), document.get("file_name") or "photo.jpg")]
                text = (message.get("caption") or "").strip()
        except (ValueError, ChannelError) as e:
            await self._say(chat_id, str(e))
            return
        if not text and not images:
            await self._say(chat_id, "I can read text, voice notes and photos here.")
            return
        try:
            reply = await self.on_message(text, images)
        except Exception as e:
            log.exception("telegram message handling failed")
            reply = f"Sorry, that failed ({type(e).__name__})."
        await self._say(chat_id, heard + reply)

    async def poll_once(self) -> int:
        updates = await self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": POLL_SECONDS,
             "allowed_updates": ["message", "callback_query"]},
            timeout=POLL_SECONDS + 10,
        )
        for update in updates or []:
            self._offset = max(self._offset, int(update["update_id"]) + 1)
            try:
                await self.handle_update(update)
            except Exception as e:
                log.warning("telegram update failed: %s", e if isinstance(e, ChannelError) else type(e).__name__)
        return len(updates or [])

    async def _poll_loop(self) -> None:
        while True:
            cfg = self._cfg()
            if not (cfg.enabled and cfg.bot_token):
                await asyncio.sleep(5)  # settings can change at runtime
                continue
            try:
                await self.poll_once()
            except ChannelError as e:
                log.warning("%s; retrying in 15s", e)
                await asyncio.sleep(15)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
