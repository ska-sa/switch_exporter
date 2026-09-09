
import asyncio
from collections.abc import Coroutine
from typing import Optional, List
import time
from typing_extensions import override
import prometheus_client
import logging

from .cache import Cache, Item
from .switch import Switch
logger = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


class Scraper(Item):
    def __init__(
        self,
        cache: Cache,
        switch: Switch,
        enable_timing_metrics: bool = True,
    ) -> None:
        super().__init__(cache, switch.hostname)
        self.enable_timing_metrics = enable_timing_metrics
        self.switch = switch
        self._lock = asyncio.Lock()
        # TODO: Use a TaskGroup instead of a list of tasks to robustly handle the async context.
        self.tasks = []
        self.done = asyncio.Event()  # Set to True when the scraper is done scraping.
        self.done.set()  # Initially set to True to indicate that the scraper should be started.
        self.registry = prometheus_client.CollectorRegistry()
        self._error = None

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

    async def wait_for_scraper(self) -> None:
        """Wait until collector tasks finish and publish completion.

        Must not raise: this runs as a background task so that a timed-out
        caller does not prevent ``done`` from being set.
        """
        try:
            if not self.tasks:
                return
            done, _ = await asyncio.wait(self.tasks)
            exceptions = []
            for task in done:
                try:
                    task.result()
                except Exception as e:
                    logger.error('Error during scraping metrics: %s', task.get_name())
                    exceptions.append(e)
            if exceptions:
                self._error = Exception(
                    "Error during scraping metrics: " + ', '.join([str(e) for e in exceptions])
                )
        except Exception as e:
            self._error = e
        finally:
            self.done.set()

    async def await_scraper_done(self, timeout: float) -> prometheus_client.CollectorRegistry:
        await asyncio.wait_for(self.done.wait(), timeout=timeout)
        if self._error is not None:
            raise self._error
        return self.registry

    async def scrape(
        self,
        timeout: float,
        collectors: Optional[List[str]],
    ) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()
        async with self._lock:
            scrape_timeout = timeout - (time.perf_counter() - start_time)
            new_scrape = self.done.is_set()
            if new_scrape:
                self.done.clear()

        if not new_scrape:
            return await self.await_scraper_done(scrape_timeout)

        self.registry = prometheus_client.CollectorRegistry()
        scrapers = []
        if collectors is None:
            for scraper in self.switch.collectors.values():
                scrapers.append(scraper(self.registry))
        else:
            for collector in collectors:
                try:
                    scrapers.append(self.switch.collectors[collector](self.registry))
                except KeyError as e:
                    raise ValidationError(f'Unknown collector: {collector}') from e

        await self.switch.refresh_port_info()

        timing_gauge = prometheus_client.Gauge(
            'switch_coroutine_duration_seconds', 'duration of the coroutine',
            labelnames=('hostname', 'coroutine'),
            registry=self.registry,
        )

        self.tasks = [
            asyncio.create_task(self.timed(s, timing_gauge, self.switch.hostname), name=s.__name__)
            for s in scrapers
        ]
        scrape_timeout = timeout - (time.perf_counter() - start_time)
        if scrape_timeout <= 0:
            raise asyncio.TimeoutError('Timed out before scraping any metrics')

        asyncio.create_task(self.wait_for_scraper())
        return await self.await_scraper_done(scrape_timeout)

    @override
    async def close(self) -> None:
        await self.switch.close()
        self.done.set()
