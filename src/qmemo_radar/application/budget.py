"""BudgetGuard: every paid external call asks it first, before any request is sent.

Free adapters never receive a guard, so an exhausted budget cannot stop them.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from qmemo_radar.application.ports import CostLedger
from qmemo_radar.domain import CostEntry
from qmemo_radar.exceptions import BudgetBlocked

logger = logging.getLogger(__name__)


class PaidFeature(StrEnum):
    X_SEARCH = "x_search"
    X_LOOKUP = "x_lookup"
    LLM = "llm"


def month_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetGuard:
    def __init__(
        self,
        ledger: CostLedger,
        *,
        enabled: frozenset[PaidFeature],
        hard_limit_usd: Decimal,
        target_usd: Decimal,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ledger = ledger
        self.enabled = enabled
        self.hard_limit_usd = hard_limit_usd
        self.target_usd = target_usd
        self._clock = clock

    async def reserve(
        self,
        feature: PaidFeature,
        *,
        provider: str,
        operation: str,
        units: int,
        cost_usd: Decimal | None,
    ) -> int:
        """Record the worst-case cost of one call or raise BudgetBlocked. Returns the entry id.

        cost_usd=None means the price is unknown, and an unknown price is never allowed.
        """
        log = {"operation": f"budget:{provider}:{operation}"}
        if feature not in self.enabled:
            code = "paid_disabled"
        elif cost_usd is None:
            code = "unknown_cost"
        else:
            now = self._clock()
            entry = CostEntry(
                provider=provider,
                operation=operation,
                units=units,
                estimated_cost_usd=cost_usd,
                created_at=now,
            )
            try:
                reserved = await self._ledger.reserve_cost(
                    entry, since=month_start(now), limit_usd=self.hard_limit_usd
                )
            except Exception as exc:
                # A locked or broken ledger must block the call, not let it through.
                logger.warning(
                    "cost ledger unavailable",
                    extra={**log, "result": "blocked", "error_code": type(exc).__name__},
                )
                raise BudgetBlocked("ledger_unavailable") from exc
            if reserved is not None:
                entry_id, month_total = reserved
                if month_total > self.target_usd:
                    logger.warning(
                        "monthly cost target exceeded",
                        extra={**log, "result": f"month_usd={month_total}"},
                    )
                return entry_id
            code = "hard_limit_reached"
        logger.warning("paid call blocked", extra={**log, "result": "blocked", "error_code": code})
        raise BudgetBlocked(code)

    async def settle(self, entry_id: int, *, units: int, cost_usd: Decimal) -> None:
        try:
            await self._ledger.settle_cost(entry_id, units=units, cost_usd=cost_usd)
        except Exception as exc:
            # The larger reservation simply stays, which errs on the expensive side.
            logger.warning(
                "cost settle failed",
                extra={"operation": "budget:settle", "error_code": type(exc).__name__},
            )

    async def spent_this_month(self) -> Decimal:
        return await self._ledger.cost_since(month_start(self._clock()))
