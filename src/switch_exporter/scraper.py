
import asyncio
from collections.abc import Coroutine
from typing import Callable, Optional, List
import time
from typing_extensions import override
import prometheus_client
import logging

from .cache import Cache, Item
from .switch import Switch
logger = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


class ScrapeError(Exception):
    pass


class Scraper(Item):
    def __init__(
        self,
        cache: Cache,
        key: str,
        switch: Switch,
        enable_timing_metrics: bool = True,
    ) -> None:
        super().__init__(cache, key)
        self.enable_timing_metrics = enable_timing_metrics
        self.switch = switch
        self._lock = asyncio.Lock()
        self._error = None
        self.registry = prometheus_client.CollectorRegistry()
        self.results_shown = True
        self.scraper_task = None
        self.timeout_counter = 0

    async def timed(
        self,
        coroutine: Coroutine,
        timing_gauge: prometheus_client.Gauge,
        hostname: str,
    ) -> None:
        start_time = time.perf_counter()
        await coroutine
        end_time = time.perf_counter()
        duration = end_time - start_time
        if self.enable_timing_metrics:
            timing_gauge.labels(hostname, coroutine.__name__).set(duration)

    async def start_collectors(self, collectors_fns: List[Callable]) -> None:
        """Start a task per collector function to update the registry.

        A timing gauge for the duration of the collectors is created. Once done,
        the `self.scraper_task` attribute is set to None.

        Must not raise: this runs as a background task, the errors should be raised when
        awaiting on task completion, possible from multiple client sessions, instead.
        """
        timing_gauge = prometheus_client.Gauge(
            'switch_coroutine_duration_seconds', 'duration of the coroutine',
            labelnames=('hostname', 'coroutine'),
            registry=self.registry,
        )
        collectors = [fn(self.registry) for fn in collectors_fns]
        # TODO: Use a TaskGroup instead of a list of tasks to robustly handle the async context.
        tasks = [
            asyncio.create_task(
                self.timed(c, timing_gauge, self.switch.hostname),
                name=f"{c.__name__}({self._cache_key})"
            ) for c in collectors
        ]
        self._error = None
        self.results_shown = False
        try:
            # NOTE: We use `asyncio.wait` instead of `asyncio.gather` to identify the tasks when
            # gathering exceptions.
            done, _ = await asyncio.wait(tasks)
            exceptions = []
            for task in done:
                try:
                    task.result()
                except Exception as e:
                    logger.error('Error during scraping metrics: %s', task.get_name())
                    exceptions.append(e)
            if exceptions:
                self._error = ScrapeError(
                    "Error during scraping metrics: " + ', '.join([str(e) for e in exceptions])
                )
        except Exception as e:
            self._error = e

    async def collectors_done(self, timeout: float) -> prometheus_client.CollectorRegistry:
        """Wait for the `self.scraper_task` to complete within the timeout given.

        Returns
        -------
        prometheus_client.CollectorRegistry
            The prometheus registry that the collectors filled with metrics as it completed or the
            previous unpresented one.

        Raises
        ----------
        TimeoutError
            When the timeout value is less than the time taken for the `self.scraper_task`
        ScrapeError
            When the `self.scraper_task` encountered an error on any of the collectors.
        RuntimeError
            When the scraper is getting timeouts in sequence an excessive amount of times (10)
        """
        try:
            if self.scraper_task is None:
                self.timeout_counter = 0
                self.results_shown = True
                return self.registry
            await asyncio.wait_for(self.scraper_task, timeout=timeout)
            if self._error is not None:
                self.results_shown = True
                raise self._error
        except asyncio.TimeoutError:
            # This prevents the scraper from getting stuck in a loop of timeouts if they are related
            # to the switch connection.
            self.timeout_counter += 1
            if self.timeout_counter >= 10:
                raise RuntimeError(
                    f'Timed out waiting for {self._cache_key} metrics '
                    f'{self.timeout_counter} times in a row'
                ) from None
            raise asyncio.TimeoutError(f'Timed out waiting for {self._cache_key} metrics') from None
        except asyncio.CancelledError:
            self.scraper_task = None
            raise
        else:
            self.scraper_task = None

        self.timeout_counter = 0
        self.results_shown = True
        return self.registry

    async def scrape(
        self,
        timeout: float,
        collectors: Optional[List[str]],
    ) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()

        await self.switch.refresh_port_info()

        scraper_fns = []
        if collectors is None:
            scraper_fns = list(self.switch.collectors.values())
        else:
            for collector in collectors:
                try:
                    scraper_fns.append(self.switch.collectors[collector])
                except KeyError as e:
                    raise ValidationError(f'Unknown collector: {collector}')

        async with self._lock:
            scrape_timeout = timeout - (time.perf_counter() - start_time)
            if self.results_shown is True and self.scraper_task is None:
                # We have already returned the results, so we need to start a
                # new task to scrape the metrics.
                self.registry = prometheus_client.CollectorRegistry()
                self.scraper_task = asyncio.create_task(
                    self.start_collectors(scraper_fns),
                    name=f'scraper_task({self._cache_key})'
                )

        return await self.collectors_done(scrape_timeout)

    @override
    async def close(self) -> None:
        await self.switch.close()
        if self.scraper_task is not None:
            self.scraper_task.cancel()
        self.registry = prometheus_client.CollectorRegistry()
        self.results_shown = True
        self.scraper_task = None
        self.timeout_counter = 0
