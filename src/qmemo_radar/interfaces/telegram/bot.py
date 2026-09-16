"""aiogram glue: turns updates into controller calls and sends replies. No decisions here."""

import logging
from collections.abc import Awaitable
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from qmemo_radar.domain import ScoredEvent
from qmemo_radar.exceptions import DeliveryFailed
from qmemo_radar.interfaces.telegram import render
from qmemo_radar.interfaces.telegram.controller import Reply, Send, TelegramController
from qmemo_radar.interfaces.telegram.render import Keyboard

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="today", description="Карточки за сегодня"),
    BotCommand(command="saved", description="Отложенные и одобренные"),
    BotCommand(command="run", description="Собрать сейчас"),
    BotCommand(command="status", description="Состояние Radar"),
    BotCommand(command="pause", description="Пауза"),
    BotCommand(command="resume", description="Продолжить"),
]


def build_bot(token: str) -> Bot:
    return Bot(
        token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def build_dispatcher(controller: TelegramController) -> Dispatcher:
    router = Router()

    @router.message()
    async def on_message(message: Message, bot: Bot) -> None:
        user_id = message.from_user.id if message.from_user else None
        send = _sender(bot, message.chat.id)
        await _guarded(controller.handle_message(user_id, message.text or "", send), send)

    @router.callback_query()
    async def on_callback(callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer()
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        send = _sender(bot, chat_id)
        await _guarded(
            controller.handle_callback(callback.from_user.id, callback.data or "", send), send
        )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


class TelegramReviewGateway:
    def __init__(self, bot: Bot, *, chat_id: int, timezone: ZoneInfo) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._timezone = timezone

    async def send_card(self, card: ScoredEvent, *, urgent: bool) -> int:
        try:
            message = await self._bot.send_message(
                self._chat_id,
                render.card_text(card, timezone=self._timezone, urgent=urgent),
                reply_markup=markup(render.card_keyboard(card.event.event_id)),
            )
        except TelegramAPIError as exc:
            raise DeliveryFailed(type(exc).__name__) from exc
        return message.message_id


def markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in keyboard
        ]
    )


def _sender(bot: Bot, chat_id: int) -> Send:
    async def send(reply: Reply) -> None:
        await bot.send_message(chat_id, reply.text, reply_markup=markup(reply.keyboard))

    return send


async def _guarded(handling: Awaitable[None], send: Send) -> None:
    """Last-resort boundary: log the failure without payloads and tell the user."""
    try:
        await handling
    except Exception as exc:
        logger.exception(
            "telegram update failed",
            extra={
                "operation": "telegram_update",
                "result": "error",
                "error_code": type(exc).__name__,
            },
        )
        try:
            await send(Reply("Внутренняя ошибка. Повторное нажатие безопасно, попробуйте ещё раз."))
        except TelegramAPIError:
            pass
