"""`qmemo-radar run`: one Telegram poller plus the scheduler, stopped cleanly by SIGTERM/SIGINT."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Sequence

import yaml
from pydantic import ValidationError

from qmemo_radar.bootstrap import build_runtime, configure_logging
from qmemo_radar.config import RadarSettings, load_sources

logger = logging.getLogger(__name__)

EXIT_CONFIG = 78  # EX_CONFIG from sysexits.h


async def run_production(settings: RadarSettings) -> int:
    configure_logging(settings)
    problems = settings.production_problems()
    for problem in problems:
        logger.error(
            "configuration problem",
            extra={"operation": "startup", "result": "invalid_config", "error_code": problem},
        )
    if problems:
        return EXIT_CONFIG
    try:
        sources = load_sources(settings.sources_path)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        logger.error(
            "sources.yaml is invalid",
            extra={
                "operation": "startup",
                "result": "invalid_config",
                "error_code": str(exc)[:300],
            },
        )
        return EXIT_CONFIG

    from qmemo_radar.interfaces.telegram.bot import prepare_bot

    stop = asyncio.Event()
    install_signal_handlers(stop)
    try:
        async with build_runtime(settings, sources) as runtime:
            await runtime.repository.initialize()
            interrupted = await runtime.repository.fail_interrupted_runs()
            logger.info(
                "radar starting",
                extra={"operation": "startup", "result": f"interrupted_runs={interrupted}"},
            )
            await prepare_bot(runtime.bot)
            await serve(
                [
                    runtime.scheduler.run,
                    lambda: runtime.dispatcher.start_polling(
                        runtime.bot,
                        handle_signals=False,
                        close_bot_session=False,
                        allowed_updates=["message", "callback_query"],
                    ),
                ],
                stop,
            )
    except Exception as exc:
        logger.exception(
            "radar stopped after a failure",
            extra={"operation": "shutdown", "result": "error", "error_code": type(exc).__name__},
        )
        return 1
    logger.info("shutdown complete", extra={"operation": "shutdown", "result": "ok"})
    return 0


async def serve(jobs: Sequence[Callable[[], Awaitable[None]]], stop: asyncio.Event) -> None:
    """Run jobs until `stop` is set or one of them fails, then cancel and await all of them."""
    tasks = [asyncio.ensure_future(job()) for job in jobs]
    stopper = asyncio.ensure_future(stop.wait())
    done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    for task in [*tasks, stopper]:
        task.cancel()
    await asyncio.gather(*tasks, stopper, return_exceptions=True)
    for task in done:
        if task is not stopper and not task.cancelled():
            raise RuntimeError("a long-running job stopped unexpectedly") from task.exception()


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # Windows development machines
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
