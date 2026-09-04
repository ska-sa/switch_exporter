import logging
import asyncio
import re
import time
from typing import Any, Coroutine, Iterable, List, Optional, Pattern, Tuple
from typing_extensions import override

import attr
import prometheus_client

from .connection import Connection

from .cache import Cache, Item
from . import metrics

logger = logging.getLogger(__name__)

_PORT_RE = re.compile(r'(?m)^Eth([^ :]*)(?: \(.*\))?:?')
_DIRECTED_PORT_RE = re.compile(r'(?m)(?:Eth:)?\s*^\s*(Rx|Tx):?\s+')
_COUNTER_RE = re.compile(r'^(\d+) +(.*)$')
_REMOTE_PORT_ID_RE = re.compile(r'^Remote port-id *: ([^;]+)(?:$| ; port id subtype:)')
_REMOTE_PORT_DESCRIPTION_RE = \
    re.compile(r'^Remote port description *: (?!Not Advertised)(?!N\\A)(.*)$')
_REMOTE_NAME_RE = re.compile(r'^Remote system name *: (?!Not Advertised)(.*)$')
_OPERATIONAL_CHANGES_RE = \
    re.compile(r'(.*) \((\d+) oper change\)')
_DIAGNOSTIC_CODE_RE = re.compile(r'^Eth\d+\/\d+\s+(\d+)')
_DIAGNOSTIC_PORT_CODE_RE = re.compile(r'(?m)^\s*Eth([^ \t:]+).*?\s+(\d+).*$')
_TRANSCEIVER_POWER_TX_RE = re.compile(r'(\w+) Tx Power\s*: .* mW / (-?\d+\.\d+) dBm')
_TRANSCEIVER_POWER_RX_RE = re.compile(r'(\w+) Rx Power\s*: .* mW / (-?\d+\.\d+) dBm')
_TRANSCEIVER_POWER_HI_RX_THRESHOLD_RE = re.compile(
    r'\s*Hi Rx Power Alarm Thresh\s*: .* mW / (-?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_LOW_RX_THRESHOLD_RE = re.compile(
    r'\s*Low Rx Power Alarm Thresh\s*: .* mW / (-?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_HI_TX_THRESHOLD_RE = re.compile(
    r'\s*Hi Tx Power Alarm Thresh\s*: .* mW / (-?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_LOW_TX_THRESHOLD_RE = re.compile(
    r'\s*Low Tx Power Alarm Thresh\s*: .* mW / (-?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_SECTION_RE = re.compile(r'Port (\d+(?:\/\d+)+) transceiver diagnostic data:')


class ValidationError(Exception):
    pass


@attr.s(slots=True)
class LLDPRemoteInfo:
    name = attr.ib(type=str, default='')
    port_id = attr.ib(type=str, default='')
    port_description = attr.ib(type=str, default='')


def split_aggregate(
    results: str,
    regex: Pattern[str],
    expected_pairs: Optional[int] = None,
) -> Iterable[Tuple[str, str]]:
    """
    Split the results into pairs of (matched_part, section_before_next_match).

    When using re.split() with capturing groups, the result alternates:
    [text_before_first_match, captured_group_1, text_after_match_1, captured_group_2,
    text_after_match_2, ...]
    Skip the first element (text before any match), then iterate in pairs

    Returns
    -------
    Iterable[Tuple[str, str]]
        An iterator of pairs of ``(matched_part, section_before_next_match)``.

    Raises
    ------
    RuntimeError
        If the number of entries doesn't match the expected number of pairs.
    """
    data = regex.split(results)
    if expected_pairs is not None and len(data) != expected_pairs * 2 + 1:
        raise RuntimeError(
            f'found {len(data)} total entries, expected {expected_pairs} pairs and one header line')
    for i in range(1, len(data) - 1, 2):
        yield data[i], data[i + 1]


class Switch(Item):
    """Collect statistics about a single switch.

    An instance has an SSH connection that is initialised on first use. It
    does not automatically reconnect: if you get a connection error, throw
    it away (via :meth:`destroy`) and create a new one.
    """

    def __init__(
        self,
        cache: Cache,
        hostname: str,
        username: str,
        password: str,
        keyfile: str,
        lldp_timeout: float,
        enable_timing_metrics: bool = True,
    ) -> None:
        super().__init__(cache, hostname)
        self.ports = []
        self.conn = Connection(hostname, username, password, keyfile)
        self.hostname = hostname
        self.lldp_info = {}
        self.lldp_time = 0.0          # time when LLDP info was last updated
        self.lldp_timeout = lldp_timeout
        self._lock = asyncio.Lock()   # Serialises port and lldp info
        self.enable_timing_metrics = enable_timing_metrics
        self.collectors = {
            'counters': self._scrape_counters,
            'state': self._scrape_state,
            'operational_changes': self._scrape_operational_changes,
            'link_diagnostic_code': self._scrape_link_diagnostic_code,
            'transceiver_power': self._scrape_transceiver_power,
        }

    def __repr__(self) -> str:
        return 'Switch({!r})'.format(self.hostname)

    async def _run_command(self, command: str) -> str:
        result = await self.conn.run_process(command)
        if not isinstance(result, str):
            raise TypeError(f'Expected str, got {type(result)}')
        return result

    async def _populate_ports(self) -> None:
        """Populate the ports list"""
        if self.ports != []:  # ports are already populated
            return
        result = await self._run_command('show interfaces ethernet status')
        for line in result.splitlines():
            fields = line.split()
            if not fields:
                continue
            match = _PORT_RE.match(fields[0])
            if match:
                self.ports.append(match.group(1))

    async def _update_lldp_periodically(self) -> None:
        """Ensure the LLDP information is up to date"""
        now = time.time()
        if now - self.lldp_time < self.lldp_timeout:
            return
        await self._update_lldp()
        self.lldp_time = now

    async def _update_lldp(self) -> None:
        logger.info('Updating LLDP information for %s', self.hostname)
        result = await self._run_command(
            'show lldp interfaces ethernet remote '
            '| include "^Eth|^ *Remote port description *:'
            '|^ *Remote system name *:|^ *Remote port-id *:"'
        )
        info = LLDPRemoteInfo()
        new_lldp = {}

        for line in result.splitlines():
            line = line.strip()
            match = _PORT_RE.match(line)
            if match:
                port = match.group(1)
                new_lldp[port] = info = LLDPRemoteInfo()
                continue
            match = _REMOTE_PORT_ID_RE.match(line)
            if match:
                info.port_id = match.group(1)
                continue
            match = _REMOTE_PORT_DESCRIPTION_RE.match(line)
            if match:
                info.port_description = match.group(1)
                continue
            match = _REMOTE_NAME_RE.match(line)
            if match:
                info.name = match.group(1)
                continue
        self.lldp_info = new_lldp

    async def _scrape_counters(self, registry: prometheus_client.CollectorRegistry) -> None:
        interface_counters = {}
        for name in metrics.COUNTERS:
            metric = metrics.name_to_metric(name)
            interface_counters[name] = prometheus_client.Counter(
                metric, 'total number of ' + name,
                labelnames=('port', 'direction', 'remote_name',
                            'remote_port_id', 'remote_port_description'),
                registry=registry
            )

        # Run once, but ensure the output for each port is identifiable by port name.
        cmd = [
            f'show interfaces ethernet {port} counters'
            for port in self.ports
        ]
        result = await self._run_command('\n'.join(cmd))
        dummy_info = LLDPRemoteInfo()

        port_number = 0
        for direction, section in split_aggregate(
            result,
            _DIRECTED_PORT_RE,
            len(self.ports) * 2
        ):
            direction = direction.lower()
            if direction == 'rx':
                port_number += 1
            port = self.ports[port_number - 1]
            info = self.lldp_info.get(port, dummy_info)
            for line in section.splitlines():
                line = line.strip()
                match = _COUNTER_RE.match(line)
                if match and match.group(2) in metrics.COUNTERS:
                    # To enable exact deltas, wrap every 2^53 so that
                    # there is no rounding in IEEE double precision.
                    count = int(match.group(1)) & (2**53 - 1)
                    name = match.group(2)
                    labels = (port, direction, info.name, info.port_id, info.port_description)
                    interface_counters[name].labels(*labels).inc(count)

    async def _scrape_state(self, registry: prometheus_client.CollectorRegistry) -> None:
        _state_labelnames = ('port', 'remote_name', 'remote_port_id', 'remote_port_description')
        port_enabled = prometheus_client.Gauge(
            'switch_port_enabled', 'whether port is administratively enabled',
            labelnames=_state_labelnames,
            registry=registry,
        )
        port_up = prometheus_client.Gauge(
            'switch_port_up', 'whether port is currently up',
            labelnames=_state_labelnames,
            registry=registry,
        )
        result = await self._run_command(r'show interfaces ethernet description')
        dummy_info = LLDPRemoteInfo()
        results = split_aggregate(result, _PORT_RE, len(self.ports))
        for port, section in results:
            sections = section.split()
            info = self.lldp_info.get(port, dummy_info)
            labels = (port, info.name, info.port_id, info.port_description)
            port_enabled.labels(*labels).set(int(sections[0] == 'Enabled'))
            port_up.labels(*labels).set(int(sections[1] == 'Up'))

    async def _scrape_operational_changes(
        self,
        registry: prometheus_client.CollectorRegistry
    ) -> None:
        _state_labelnames = ('port', 'remote_name', 'remote_port_id', 'remote_port_description')
        port_operational_changes = prometheus_client.Counter(
            'switch_port_operational_changes_total', 'total number of operational changes',
            labelnames=_state_labelnames,
            registry=registry
        )

        # Include the port header lines so we can associate each result with its port.
        cmd = (
            r'show interfaces ethernet '
            r'| include "^Eth|^\s+Last change in operational status: |^"'
        )
        result = await self._run_command(cmd)
        for port, section in split_aggregate(result, _PORT_RE, len(self.ports)):
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)

            # Find the single operational status change line in this section.
            count = 0
            for line in section.splitlines():
                line = line.strip()
                match = _OPERATIONAL_CHANGES_RE.match(line)
                if match:
                    count = int(match.group(2))
                    break

            port_operational_changes.labels(*labels).inc(count)

    async def _scrape_link_diagnostic_code(
        self,
        registry: prometheus_client.CollectorRegistry
    ) -> None:
        _state_labelnames = ('port', 'remote_name', 'remote_port_id', 'remote_port_description')
        port_link_diagnostic_code = prometheus_client.Gauge(
            'switch_port_link_diagnostic_code', 'state of the link',
            labelnames=_state_labelnames,
            registry=registry
        )

        cmd = r'show interfaces ethernet link-diagnostics | include "^\s+Eth"'
        result = await self._run_command(cmd)
        for match in _DIAGNOSTIC_PORT_CODE_RE.finditer(result):
            port = match.group(1)
            if port not in self.ports:
                continue
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            port_link_diagnostic_code.labels(*labels).set(int(match.group(2)))
        # No assertions here because some switches don't support link diagnostics

    async def _scrape_transceiver_power(
        self,
        registry: prometheus_client.CollectorRegistry
    ) -> None:
        port_transceiver_power = prometheus_client.Gauge(
            'switch_port_transceiver_power_dbm', 'power of the tx channel in decibel milliwatts',
            labelnames=(
                'port', 'remote_name', 'remote_port_id', 'remote_port_description',
                'channel', 'direction'
            ),
            registry=registry
        )
        port_transceiver_hi_power_alarm_threshold = prometheus_client.Gauge(
            'switch_port_transceiver_hi_power_alarm_threshold_dbm',
            'hi power alarm threshold in decibel milliwatts',
            labelnames=(
                'port', 'remote_name', 'remote_port_id', 'remote_port_description',
                'direction'
            ),
            registry=registry
        )
        port_transceiver_low_power_alarm_threshold = prometheus_client.Gauge(
            'switch_port_transceiver_low_power_alarm_threshold_dbm',
            'low power alarm threshold in decibel milliwatts',
            labelnames=(
                'port', 'remote_name', 'remote_port_id', 'remote_port_description',
                'direction'
            ),
            registry=registry
        )
        result = await self._run_command(
            "enable\nshow interfaces ethernet transceiver diagnostics"
        )
        for port, section in split_aggregate(result, _TRANSCEIVER_POWER_SECTION_RE):
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)

            matches = _TRANSCEIVER_POWER_RX_RE.finditer(section)
            match = None
            for match in matches:
                child = port_transceiver_power.labels(*labels, match.group(1), 'rx')
                child.set(float(match.group(2)))

            matches = _TRANSCEIVER_POWER_TX_RE.finditer(section)
            for match in matches:
                child = port_transceiver_power.labels(*labels, match.group(1), 'tx')
                child.set(float(match.group(2)))

            match = _TRANSCEIVER_POWER_HI_RX_THRESHOLD_RE.search(section)
            if match:
                child = port_transceiver_hi_power_alarm_threshold.labels(*labels, 'rx')
                child.set(float(match.group(1)))

            match = _TRANSCEIVER_POWER_LOW_RX_THRESHOLD_RE.search(section)
            if match:
                child = port_transceiver_low_power_alarm_threshold.labels(*labels, 'rx')
                child.set(float(match.group(1)))

            match = _TRANSCEIVER_POWER_HI_TX_THRESHOLD_RE.search(section)
            if match:
                child = port_transceiver_hi_power_alarm_threshold.labels(*labels, 'tx')
                child.set(float(match.group(1)))

            match = _TRANSCEIVER_POWER_LOW_TX_THRESHOLD_RE.search(section)
            if match:
                child = port_transceiver_low_power_alarm_threshold.labels(*labels, 'tx')
                child.set(float(match.group(1)))
        # No assertions here because some switches don't support transceiver power

    async def timed(
        self,
        coroutine: Coroutine[Any, Any, None],
        timing_gauge: prometheus_client.Gauge
    ) -> None:
        start_time = time.perf_counter()
        await coroutine
        end_time = time.perf_counter()
        duration = end_time - start_time
        if self.enable_timing_metrics:
            timing_gauge.labels(self.hostname, coroutine.__name__).set(duration)

    async def scrape(
        self,
        timeout: float,
        collectors: Optional[List[str]]
    ) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()
        registry = prometheus_client.CollectorRegistry()
        scrapers = []
        if collectors is None:
            for scraper in self.collectors.values():
                scrapers.append(scraper(registry))
        else:
            for collector in collectors:
                try:
                    scrapers.append(self.collectors[collector](registry))
                except KeyError as e:
                    raise ValidationError(f'Unknown collector: {collector}') from e

        async with self._lock:
            await self._populate_ports()
            await self._update_lldp_periodically()

        timing_gauge = prometheus_client.Gauge(
            'switch_coroutine_duration_seconds', 'duration of the coroutine',
            labelnames=('hostname', 'coroutine'),
            registry=registry,
        )

        # TODO: Use a TaskGroup instead of a list of tasks to robustly handle the async context.
        tasks = [asyncio.create_task(self.timed(s, timing_gauge), name=s.__name__)
                 for s in scrapers]
        scrape_timeout = timeout - (time.perf_counter() - start_time)
        if scrape_timeout <= 0:
            raise asyncio.TimeoutError('Timed out before scraping any metrics')
        done, pending = await asyncio.wait(tasks, timeout=scrape_timeout)
        exceptions = []
        for task in pending:
            logger.error('[%s] Cancelling scraping metrics: %s after %s seconds',
                         self.hostname, task.get_name(), timeout)
            task.cancel()
        for task in done:
            try:
                task.result()
            except Exception as e:
                logger.error('Error during scraping metrics: %s', task.get_name())
                exceptions.append(e)

        if exceptions:
            raise Exception(
                "Error during scraping metrics: " + ', '.join([str(e) for e in exceptions])
            )
        return registry

    @override
    async def close(self) -> None:
        await self.conn.close()
