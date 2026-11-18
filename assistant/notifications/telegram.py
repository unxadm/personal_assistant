"""Telegram: a notification channel and a chat interface, via the Bot API.

One bot serves the whole server (its token is a server-wide setting). Each user links their
own private chat to their account by entering the chat id in their settings; the bot hands
every incoming update to the user that chat belongs to, and ignores everyone else. A chat id
can be linked to one account only.

Uses long polling, so the server does not need to be reachable from the internet.
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
    """One user's Telegram: where their notifications go and how their chat is handled."""

    name = "telegram"
    label = "Telegram"

    def __init__(self, bot: TelegramBot, get_config: Callable[[], AppConfig]):
        self._bot = bot
        self._get_config = get_config
        # Wired up by the app: chat text -> reply, and (ref, action) -> confirmation.
        self.on_message: Callable[[str, list[tuple[bytes, str]]], Awaitable[str]] | None = None
        # Voice note bytes -> transcript (raises ValueError with a message for the user).
        self.on_voice: Callable[[bytes], Awaitable[str]] | None = None
        self.on_action: Callable[[str, str], Awaitable[str]] | None = None

    def app_config(self) -> AppConfig:
        return self._get_config()

    def cfg(self) -> TelegramConfig:
        return self._get_config().notifications.telegram

    async def status(self) -> dict[str, Any]:
        cfg = self.cfg()
        if not cfg.enabled:
            return {"ready": False, "detail": "turned off"}
        if not self._bot.token():
            return {"ready": False, "detail": "missing: bot token (ask an admin to set it up)"}
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
        payload: dict[str, Any] = {"chat_id": self.cfg().chat_id, "text": text[:MAX_TEXT]}
        if notification.ref and notification.actions:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": a["label"], "callback_data": f"{a['id']}|{notification.ref}"[:64]}
                        for a in notification.actions
                    ]
                ]
            }
        await self._bot.call("sendMessage", payload)


class TelegramBot:
    """The server's one bot: the API client, and the loop that routes updates to users."""

    def __init__(self, http: httpx.AsyncClient, get_config: Callable[[], AppConfig]):
        self._http = http
        self._get_config = get_config
        # Wired up by the app. ``resolve(chat_id)`` is the channel of the one user who linked
        # that chat; ``linking_open()`` is true while someone has Telegram on but no chat yet;
        # ``wanted()`` is true while any user has Telegram turned on.
        self.resolve: Callable[[str], TelegramChannel | None] = lambda chat_id: None
        self.linking_open: Callable[[], bool] = lambda: False
        self.wanted: Callable[[], bool] = lambda: False
        self._offset = 0
        self._task: asyncio.Task[None] | None = None

    def _cfg(self) -> TelegramConfig:
        return self._get_config().notifications.telegram

    def token(self) -> str | None:
        return self._cfg().bot_token

    async def call(self, method: str, payload: dict[str, Any], timeout: float = 20.0) -> Any:
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

    async def _download(self, file_id: str, max_mb: float) -> bytes:
        cfg = self._cfg()
        info = await self.call("getFile", {"file_id": file_id})
        limit = max_mb * 1024 * 1024
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
            await self.call("sendMessage", {"chat_id": chat_id, "text": text[start : start + MAX_TEXT] or "…"})

    # -- incoming ---------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
            user = self.resolve(chat_id) if chat_id else None
            if user is None:
                return
            action, _, ref = str(callback.get("data") or "").partition("|")
            answer = await user.on_action(ref, action) if user.on_action and ref else "Unknown action."
            await self.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": answer[:200]})
            message = callback.get("message") or {}
            if message.get("message_id"):
                try:  # remove the buttons so they cannot be pressed twice
                    await self.call(
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
        user = self.resolve(chat_id)
        if user is None:
            # Not linked to anyone: tell the sender their own id, so they can enter it in
            # their settings, but only while someone is waiting to link a chat.
            if text.startswith("/start") and self.linking_open():
                await self._say(
                    chat_id,
                    f"This assistant is not linked to a chat yet. Your chat id is {chat_id}. "
                    "Enter it under Settings → Notifications → Telegram.",
                )
            return  # strangers get no other response at all
        cfg = user.cfg()
        voice = message.get("voice") or message.get("audio")
        document = message.get("document") or {}
        photo = (message.get("photo") or [None])[-1]  # sizes are listed smallest first
        if photo is None and str(document.get("mime_type", "")).startswith("image/"):
            photo = document
        if text.startswith("/start") and not (voice or photo):
            await self._say(chat_id, "Connected. Send a message, voice note or photo, or /new to start a fresh conversation.")
            return
        if not cfg.chat or user.on_message is None:
            await self._say(chat_id, "Chat over Telegram is turned off; I only send notifications here.")
            return

        max_audio = user.app_config().uploads.max_audio_mb
        heard = ""
        images: list[tuple[bytes, str]] = []
        try:
            if voice:
                if user.on_voice is None:
                    raise ValueError("Voice notes are not available.")
                text = await user.on_voice(await self._download(voice["file_id"], max_audio))
                if not text:
                    raise ValueError("I couldn't make out any words in that.")
                heard = f"🎤 “{text}”\n\n"  # show what was understood
            elif photo:
                images = [(await self._download(photo["file_id"], max_audio), document.get("file_name") or "photo.jpg")]
                text = (message.get("caption") or "").strip()
        except (ValueError, ChannelError) as e:
            await self._say(chat_id, str(e))
            return
        if not text and not images:
            await self._say(chat_id, "I can read text, voice notes and photos here.")
            return
        try:
            reply = await user.on_message(text, images)
        except Exception as e:
            log.exception("telegram message handling failed")
            reply = f"Sorry, that failed ({type(e).__name__})."
        await self._say(chat_id, heard + reply)

    async def poll_once(self) -> int:
        updates = await self.call(
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
            if not (self._cfg().bot_token and self.wanted()):
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
