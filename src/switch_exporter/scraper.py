
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
        self.done = True
        self.registry = prometheus_client.CollectorRegistry()
        self.exceptions: list[Exception] = []

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
        done, pending = await asyncio.wait(self.tasks, timeout=self.timeout)
        if pending:
            raise asyncio.TimeoutError('Timed out waiting for tasks to complete, '
                                       f'scraper still running, pending tasks: {pending}')

        for task in done:
            try:
                task.result()
            except Exception as e:
                logger.error('Error during scraping metrics: %s', task.get_name())
                self.exceptions.append(e)

        self.done = True

    async def scrape(
        self,
        timeout: float,
        collectors: Optional[List[str]],
    ) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()

        if not self.done:
            await self.wait_for_scraper()
            return self.registry

        self.done = False
        self.timeout = timeout  # set timeout dynamically for scrape from params
        self.exceptions.clear()
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

        async with self._lock:
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

        if self.exceptions:
            raise Exception(
                "Error during scraping metrics: " + ', '.join([str(e) for e in self.exceptions])
            )
        await self.wait_for_scraper()
        return self.registry

    @override
    async def close(self) -> None:
        await self.switch.close()
        self.done = True
