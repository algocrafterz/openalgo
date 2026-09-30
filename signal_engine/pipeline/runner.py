"""Runner lifecycle: warm up, then run one cycle per bar close until the entry window ends.

Modes differ only in the injected clock, source and sink. In shadow mode (a sink that does
not dispatch) the strategy may still be signal_source pine, which lets the runner be compared
against the Pine alerts before anything is switched over.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from loguru import logger

from signal_engine.config import RunnerSettings
from signal_engine.pipeline import single_source
from signal_engine.pipeline.bars import BarSource
from signal_engine.pipeline.clock import Clock, next_cycle
from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.cycle import CycleDeps, CycleReport, run_cycle, shared_executor
from signal_engine.pipeline.exit_driver import ExitDriver
from signal_engine.pipeline.sink import ExecutionSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategy import Fire, Strategy


class Runner:
    def __init__(
        self,
        *,
        clock: Clock,
        source: BarSource,
        sink: ExecutionSink,
        strategy: Strategy,
        context: RollingContext,
        state: RunnerStateStore,
        cfg: RunnerSettings,
        symbols: list[str],
        mode: str,
        product: str,
        stale_limit_s: float,
        halted: Callable[[], bool],
        price_band: tuple[float, float],
        executor: ThreadPoolExecutor | None = None,
        exits: ExitDriver | None = None,
        manage_until_min: int | None = None,
    ) -> None:
        self.clock = clock
        self.sink = sink
        self.cfg = cfg
        self.strategy = strategy
        self.stop_after: pd.Timestamp | None = None  # test/ops hook: last bar_close to run
        self.fires: list[Fire] = []
        self.reports: list[CycleReport] = []
        self._executor = executor
        self._manage_until_min = manage_until_min
        self._deps = CycleDeps(
            strategy=strategy,
            source=source,
            context=context,
            sink=sink,
            state=state,
            clock=clock,
            symbols=list(symbols),
            cfg=cfg,
            mode=mode,
            product=product,
            stale_limit_s=stale_limit_s,
            halted=halted,
            price_band=price_band,
            executor=executor,
            exits=exits,
        )

    async def run(self, day: dt.date) -> None:
        single_source.assert_can_dispatch(self.cfg, self.strategy.name, self.sink.dispatches)
        await self._warm(day)
        start, cutoff = self.strategy.entry_window_min
        interval = self.strategy.interval_min
        while True:
            bar_close, run_at = next_cycle(self.clock.now(), interval, self.cfg.bar_delay_seconds)
            bar_open = bar_close - pd.Timedelta(minutes=interval)
            minutes = bar_open.hour * 60 + bar_open.minute
            # Entries stop at the cutoff; with an exit driver the loop keeps stepping open
            # positions until manage_until_min (the time exit), then hands over to it.
            last_min = cutoff if self._manage_until_min is None else self._manage_until_min
            if bar_open.date() != day or minutes >= last_min:
                break
            if self.stop_after is not None and bar_close > self.stop_after:
                break
            await self.clock.sleep_until(run_at)
            if minutes < start:
                continue
            report = await run_cycle(bar_close, self._deps)
            self.reports.append(report)
            self.fires.extend(report.fires)
        logger.info(f"runner finished for {day}: {len(self.reports)} cycle(s)")

    async def _warm(self, day: dt.date) -> None:
        loop = asyncio.get_running_loop()
        pool = self._executor or shared_executor()
        ctx = self._deps.context
        failed = await loop.run_in_executor(pool, ctx.warm, self._deps.symbols, day)
        logger.info(
            f"runner warm-up finished: {len(ctx.warmed)} ok, {len(failed)} failed"
            + (f" ({', '.join(failed)})" if failed else "")
        )
