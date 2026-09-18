
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
        key: str,
        switch: Switch,
        enable_timing_metrics: bool = True,
    ) -> None:
        super().__init__(cache, key)
        self.enable_timing_metrics = enable_timing_metrics
        self.switch = switch
        self._lock = asyncio.Lock()
        # TODO: Use a TaskGroup instead of a list of tasks to robustly handle the async context.
        self.tasks = []
        self.done = asyncio.Event()  # Set to True when the scraper is done scraping.
        self.done.set()  # Initially set to True to indicate that the scraper should be started.
        self.registry = prometheus_client.CollectorRegistry()
        self._error = None
        # if we timout too many times, we should raise an error and reset scraper
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

    async def wait_for_scraper(self) -> None:
        """Wait until collector tasks finish and update the registry.
        Sets the self.done event when the scraper is done.

        Must not raise: this runs as a background task so that a timed-out
        caller does not prevent ``done`` from being set.
        """
        self._error = None
        try:
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
        if self._error is not None:
            raise self._error
        try:
            await asyncio.wait_for(self.done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.timeout_counter += 1
            if self.timeout_counter > 10:
                raise RuntimeError(f'Timed out waiting for {self.cache_key} metrics {self.timeout_counter} times')
            raise asyncio.TimeoutError(f'Timed out waiting for {self.cache_key} metrics')
        except Exception as e:
            raise e

        self.timeout_counter = 0
        return self.registry

    async def scrape(
        self,
        timeout: float,
        collectors: Optional[List[str]],
    ) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()

        await self.switch.refresh_port_info()
        temp_registry = prometheus_client.CollectorRegistry()
        timing_gauge = prometheus_client.Gauge(
            'switch_coroutine_duration_seconds', 'duration of the coroutine',
            labelnames=('hostname', 'coroutine'),
            registry=temp_registry,
        )
        if collectors is None:
            scraper_fns = list(self.switch.collectors.values())
        else:
            scraper_fns = []
            for collector in collectors:
                try:
                    scraper_fns.append(self.switch.collectors[collector])
                except KeyError as e:
                    raise ValidationError(f'Unknown collector: {collector}') from e

        async with self._lock:
            scrape_timeout = timeout - (time.perf_counter() - start_time)
            new_scrape = self.done.is_set()
            if new_scrape:
                scrapers = [fn(temp_registry) for fn in scraper_fns]
                self.tasks = [
                    asyncio.create_task(
                        self.timed(s, timing_gauge, self.switch.hostname),
                        name=s.__name__ + f'({self.cache_key})'
                    )
                    for s in scrapers
                ]
                self.registry = temp_registry
                self.done.clear()

        if new_scrape:
            asyncio.create_task(self.wait_for_scraper(), name=f'wait_for_scraper({self.cache_key})')

        return await self.await_scraper_done(scrape_timeout)

    @override
    async def close(self) -> None:
        await self.switch.close()
        self.done.set()
        self.timeout_counter = 0
