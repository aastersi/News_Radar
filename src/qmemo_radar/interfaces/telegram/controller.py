"""Framework-free Telegram controller: authorization, parsing and replies. No business rules."""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from qmemo_radar.application.drafting import (
    DraftOutcome,
    DraftResult,
    DraftService,
    RevisionMode,
)
from qmemo_radar.application.normalization import parse_x_status_url
from qmemo_radar.application.review import Outcome, ReviewService
from qmemo_radar.interfaces.telegram import render
from qmemo_radar.interfaces.telegram.render import Keyboard

logger = logging.getLogger(__name__)

_ITEM_ID = re.compile(r"[0-9a-f]{32}")
_DECIDED = "Решение по этому событию уже принято, кнопка больше не действует."
_NOT_FOUND = "Событие не найдено."
_STALE_BUTTON = "Кнопка устарела."
_DRAFT_MESSAGES = {
    DraftOutcome.IN_PROGRESS: "Черновик уже готовится, подождите.",
    DraftOutcome.NOT_FOUND: "Черновик или событие не найдены.",
    DraftOutcome.CLOSED: "Событие уже закрыто, черновик не нужен.",
    DraftOutcome.STALE: "Эта версия черновика устарела или решение уже принято.",
    DraftOutcome.LIMIT_REACHED: "Переделка уже использована: доступна только одна.",
    DraftOutcome.FAILED: (
        "Не удалось подготовить черновик с точной цитатой из источника. Попробуйте ещё раз позже."
    ),
}
_REVISIONS = {"short": RevisionMode.SHORTER, "angle": RevisionMode.ANGLE}


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    keyboard: Keyboard | None = None


Send = Callable[[Reply], Awaitable[None]]


class TelegramController:
    def __init__(
        self,
        *,
        allowed_user_id: int,
        review: ReviewService,
        drafts: DraftService,
        timezone: ZoneInfo,
    ) -> None:
        self._allowed_user_id = allowed_user_id
        self._review = review
        self._drafts = drafts
        self._timezone = timezone

    async def handle_message(self, user_id: int | None, text: str, send: Send) -> None:
        if not await self._authorized(user_id, send):
            return
        words = text.strip().split(maxsplit=1)
        command = words[0].split("@", 1)[0].lower() if words else ""
        if command in ("/start", "/help"):
            await send(Reply(render.HELP_TEXT))
        elif command == "/today":
            report = await self._review.today()
            await send(Reply(render.today_text(report, timezone=self._timezone)))
        elif command == "/saved":
            await send(Reply(render.saved_text(await self._review.snoozed())))
        elif command in ("/pause", "/resume"):
            await self._review.set_paused(command == "/pause")
            state = "поставлен на паузу" if command == "/pause" else "снова работает"
            await send(Reply(f"Radar {state}."))
        elif parse_x_status_url(text):
            await send(Reply(_link_reply(await self._review.submit_link(text))))
        elif command.startswith("/") or not command:
            await send(Reply(render.HELP_TEXT))
        else:
            await self._revise_by_instruction(text.strip(), send)

    async def handle_callback(self, user_id: int | None, data: str, send: Send) -> None:
        if not await self._authorized(user_id, send):
            return
        parts = data.split(":")
        if len(parts) != 3 or not _ITEM_ID.fullmatch(parts[2]):
            await send(Reply(_STALE_BUTTON))
            return
        scope, action, item_id = parts
        if scope == "e" and action == "why":
            card = await self._review.explain(item_id)
            await send(Reply(render.score_text(card) if card else _NOT_FOUND))
        elif scope == "e" and action == "skip":
            outcome = await self._review.skip(item_id, self._allowed_user_id)
            await send(Reply(_decision_reply(outcome, "⏭ Пропущено.")))
        elif scope == "e" and action == "later":
            outcome = await self._review.later(item_id, self._allowed_user_id)
            await send(Reply(_decision_reply(outcome, "🕒 Отложено до следующей подборки.")))
        elif scope == "e" and action == "use":
            await send(Reply("⏳ Готовлю черновик…"))
            await self._send_draft(await self._drafts.use(item_id, self._allowed_user_id), send)
        elif scope == "d" and action in _REVISIONS:
            await send(Reply("⏳ Переделываю…"))
            result = await self._drafts.revise(item_id, self._allowed_user_id, _REVISIONS[action])
            await self._send_draft(result, send)
        elif scope == "d" and action == "ver":
            await self._send_draft(await self._drafts.verify(item_id, self._allowed_user_id), send)
        elif scope == "d" and action == "rej":
            result = await self._drafts.reject(item_id, self._allowed_user_id)
            if result.outcome is DraftOutcome.CREATED:
                await send(Reply("❌ Черновик отклонён, событие пропущено."))
            else:
                await send(Reply(_DRAFT_MESSAGES.get(result.outcome, _STALE_BUTTON)))
        else:
            await send(Reply(_STALE_BUTTON))

    async def _revise_by_instruction(self, text: str, send: Send) -> None:
        draft = await self._drafts.revisable_draft()
        if draft is None:
            await send(Reply(render.HELP_TEXT))
            return
        await send(Reply("⏳ Переделываю по вашей инструкции…"))
        result = await self._drafts.revise(
            draft.draft_id, self._allowed_user_id, RevisionMode.CUSTOM, text[:500]
        )
        await self._send_draft(result, send)

    async def _send_draft(self, result: DraftResult, send: Send) -> None:
        draft = result.draft
        if result.outcome in (DraftOutcome.CREATED, DraftOutcome.EXISTS) and draft is not None:
            card = await self._review.explain(draft.event_id)
            if card is not None:
                await send(Reply(render.draft_text(draft, card), render.draft_keyboard(draft)))
                return
        await send(Reply(_DRAFT_MESSAGES.get(result.outcome, _STALE_BUTTON)))

    async def _authorized(self, user_id: int | None, send: Send) -> bool:
        if user_id is not None and user_id == self._allowed_user_id:
            return True
        logger.warning(
            "unauthorized telegram update", extra={"operation": "auth", "result": "denied"}
        )
        await send(Reply(f"Нет доступа. Ваш Telegram ID: {user_id}"))
        return False


def _decision_reply(outcome: Outcome, done: str) -> str:
    if outcome is Outcome.DONE:
        return done
    return _NOT_FOUND if outcome is Outcome.NOT_FOUND else _DECIDED


def _link_reply(outcome: Outcome) -> str:
    return {
        Outcome.DONE: "Ссылка добавлена. Она будет оценена при следующем сборе.",
        Outcome.ALREADY_DECIDED: "Эта публикация уже есть в Radar.",
        Outcome.INVALID: "Нужна ссылка вида https://x.com/имя/status/123.",
    }.get(outcome, "Не удалось загрузить публикацию из X. Попробуйте позже.")
