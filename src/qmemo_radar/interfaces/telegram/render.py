"""Telegram HTML rendering. Every external value is escaped here."""

import html
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from qmemo_radar.application.drafting import MAX_DRAFT_VERSIONS
from qmemo_radar.application.review import TodayReport
from qmemo_radar.domain import (
    Draft,
    EventStatus,
    FactCheckStatus,
    ScoredEvent,
    ScoreResult,
)

Button = tuple[str, str]
Keyboard = tuple[tuple[Button, ...], ...]
CALLBACK_LIMIT_BYTES = 64

HELP_TEXT = (
    "<b>QMemo News Radar</b>\n"
    "/today — карточки за сегодня и очередь\n"
    "/saved — отложенные события\n"
    "/pause — остановить автоматический сбор и отправку\n"
    "/resume — продолжить\n\n"
    "Можно прислать ссылку вида https://x.com/имя/status/123 — она будет оценена "
    "при следующем сборе.\n"
    "Пока открыт первый черновик, обычное сообщение считается инструкцией "
    "для единственной переделки."
)

_FORMATS = {
    "quote_card": "карточка цитаты",
    "prediction_tracker": "отслеживание прогноза",
    "thread": "тред",
    "short_post": "короткий пост",
}
_ACTIONS = {
    "save_quote": "сохранить цитату",
    "share_quote": "поделиться цитатой",
    "follow_prediction": "следить за прогнозом",
    "open_qmemo": "открыть QMemo",
}
_STATUSES = {
    EventStatus.NOTIFIED: "ждёт решения",
    EventStatus.SNOOZED: "отложено",
    EventStatus.SKIPPED: "пропущено",
    EventStatus.DRAFTED: "черновик",
    EventStatus.APPROVED: "одобрено",
    EventStatus.EXPIRED: "устарело",
}
_COMPONENTS = (
    ("qmemo_relevance", "Связь с QMemo", 30),
    ("quote_strength", "Сила цитаты", 20),
    ("discussion_potential", "Потенциал обсуждения", 15),
    ("freshness", "Свежесть", 15),
    ("clarity", "Понятность", 10),
    ("action_likelihood", "Вероятность действия", 10),
)


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def callback_data(scope: str, action: str, item_id: str) -> str:
    data = f"{scope}:{action}:{item_id}"
    if len(data.encode("utf-8")) > CALLBACK_LIMIT_BYTES:
        raise ValueError("Telegram callback data is limited to 64 bytes")
    return data


def card_keyboard(event_id: str) -> Keyboard:
    return (
        (
            ("✅ Использовать", callback_data("e", "use", event_id)),
            ("⏭ Пропустить", callback_data("e", "skip", event_id)),
        ),
        (
            ("🕒 Позже", callback_data("e", "later", event_id)),
            ("❓ Почему такой балл?", callback_data("e", "why", event_id)),
        ),
    )


def draft_keyboard(draft: Draft) -> Keyboard:
    first: tuple[Button, ...] = (("✅ Принято", callback_data("d", "ok", draft.draft_id)),)
    if draft.fact_check_status is FactCheckStatus.NEEDS_REVIEW:
        first += (("🔎 Проверено вручную", callback_data("d", "ver", draft.draft_id)),)
    rows: list[tuple[Button, ...]] = [first]
    if draft.version < MAX_DRAFT_VERSIONS:
        rows.append(
            (
                ("✂️ Переделать короче", callback_data("d", "short", draft.draft_id)),
                ("🔄 Другой угол", callback_data("d", "angle", draft.draft_id)),
            )
        )
    rows.append((("❌ Отказаться", callback_data("d", "rej", draft.draft_id)),))
    return tuple(rows)


def draft_text(draft: Draft, card: ScoredEvent) -> str:
    event, score = card.event, card.score
    verified = draft.fact_check_status is FactCheckStatus.VERIFIED
    lines = [
        f"📝 <b>Черновик v{draft.version}</b> · {esc(score.headline)}",
        "",
        f"<b>Цитата</b> ({esc(draft.quote_language)}):",
        f"<blockquote>{esc(draft.quote_text)}</blockquote>",
        f"— {esc(draft.quote_author)}",
        f"🔗 {link(str(event.url), 'Источник')} · ID {esc(event.external_id)}",
        "",
        f"<b>Контекст:</b> {esc(draft.context_summary)}",
        f"<b>Текст для QMemo:</b>\n{esc(draft.qmemo_text)}",
        f"<b>X — основной:</b>\n{esc(draft.x_text_template)}",
        f"<b>X — короткий:</b>\n{esc(draft.x_text_short)}",
        f"<b>Угол:</b> {esc(draft.angle)}",
        f"<b>Формат:</b> {esc(_label(_FORMATS, score.recommended_format))}",
        f"<b>Призыв:</b> {esc(draft.cta)}",
        "<b>Проверка фактов:</b> " + ("✅ проверено" if verified else "⚠️ требуется проверка"),
    ]
    lines += [f"• {esc(note)}" for note in draft.fact_check_notes]
    lines += [
        f"Переделок осталось: {MAX_DRAFT_VERSIONS - draft.version}",
        "<i>{qmemo_url} будет заменён реальной ссылкой QMemo. Публикация выключена.</i>",
    ]
    return "\n".join(lines)


def card_text(card: ScoredEvent, *, timezone: ZoneInfo, urgent: bool) -> str:
    event, score = card.event, card.score
    author = event.author_display_name or event.author_handle or "автор неизвестен"
    handle = f" @{event.author_handle}" if event.author_handle else ""
    lines = ["⚡ <b>Срочно</b>"] if urgent else []
    lines += [
        f"<b>{esc(score.headline or event.original_text[:80])}</b>",
        esc(score.summary or event.original_text[:280]),
        "",
        f"👤 {esc(author)}{esc(handle)} · 🕒 {_local_time(card, timezone)}",
        f"🔗 {link(str(event.url), 'Открыть публикацию')}",
        f"📊 Балл: <b>{score.total}</b>/100",
        f"💡 {esc(score.rationale)}",
        f"🧩 Формат: {esc(_label(_FORMATS, score.recommended_format))}"
        f" · 🎯 Действие: {esc(_label(_ACTIONS, score.target_action))}",
        f"⚠️ {esc(risk_warning(score))}",
    ]
    return "\n".join(lines)


def risk_warning(score: ScoreResult) -> str:
    if score.fact_check_required:
        return score.fact_check_note or "Факты нужно проверить перед публикацией."
    if score.breakdown.risk_penalty:
        return f"Есть риск: штраф {score.breakdown.risk_penalty} баллов."
    return "Риск не выявлен."


def score_text(card: ScoredEvent) -> str:
    score = card.score
    values = score.breakdown.model_dump()
    lines = [f"<b>Почему {score.total}/100</b>", esc(score.headline)]
    lines += [f"• {label}: {values[name]}/{maximum}" for name, label, maximum in _COMPONENTS]
    lines += [
        f"• Штраф за риск: −{score.breakdown.risk_penalty}/30",
        f"Итог считает код: сумма компонентов минус штраф = <b>{score.total}</b>",
        f"💡 {esc(score.rationale)}",
        f"⚠️ {esc(risk_warning(score))}",
        f"Модель: {esc(score.model_name)} · промпт: {esc(score.prompt_version)}",
    ]
    return "\n".join(lines)


def today_text(report: TodayReport, *, timezone: ZoneInfo) -> str:
    lines = [
        f"<b>Сегодня отправлено: {len(report.delivered)}</b>",
        f"В очереди: {report.waiting} · осталось карточек на сегодня: {report.remaining}",
    ]
    lines += [_list_line(card) for card in report.delivered]
    return "\n".join(lines)


def saved_text(snoozed: Sequence[ScoredEvent]) -> str:
    lines = [f"<b>Отложено: {len(snoozed)}</b>"]
    lines += [_list_line(card) for card in snoozed]
    return "\n".join(lines)


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def _list_line(card: ScoredEvent) -> str:
    status = _STATUSES.get(card.event.status, card.event.status.value)
    title = card.score.headline or card.event.original_text[:60]
    return f"• {card.score.total} · {esc(status)} · {link(str(card.event.url), title)}"


def _local_time(card: ScoredEvent, timezone: ZoneInfo) -> str:
    return card.event.published_at.astimezone(timezone).strftime("%d.%m %H:%M")


def _label(labels: dict[str, str], value: str) -> str:
    return labels.get(value, value)
