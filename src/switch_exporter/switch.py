import logging
import asyncio
import re
import time
from typing import Any, Coroutine, Dict, List, Union
from typing_extensions import override

import attr
import prometheus_client

from .connection_factory import ConnectionFactory, Connection

from .cache import Cache, Item
from . import metrics

logger = logging.getLogger(__name__)

_PORT_RE = re.compile(r'^Eth([^ :]*)(?: \(.*\))?:?$')
_COUNTER_RE = re.compile(r'^(\d+) +(.*)$')
_REMOTE_PORT_ID_RE = re.compile(r'^Remote port-id *: ([^;]+)(?:$| ; port id subtype:)')
_REMOTE_PORT_DESCRIPTION_RE = \
    re.compile(r'^Remote port description *: (?!Not Advertised)(?!N\\A)(.*)$')
_REMOTE_NAME_RE = re.compile(r'^Remote system name *: (?!Not Advertised)(.*)$')
_OPERATIONAL_CHANGES_RE = \
    re.compile(r'(.*) \((\d+) oper change\)')
_OPERATIONAL_CHANGES_NEVER_RE = re.compile(r'(.*)Never')
_DIAGNOSTIC_CODE_RE = re.compile(r'^Eth\d+\/\d+\s+(\d+)')
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
_TRANSCEIVER_POWER_SECTION_RE = re.compile(r'Port (.*) transceiver diagnostic data:')
_LAST_LOGIN_RE = re.compile(r'\s*Last login: .*')
_TOTAL_CONNECTIONS_SINCE_RE = re.compile(r'\s*Number of total successful connections since last .*')


@attr.s(slots=True)
class LLDPRemoteInfo:
    name = attr.ib(type=str, default='')
    port_id = attr.ib(type=str, default='')
    port_description = attr.ib(type=str, default='')


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
        lldp_timeout: float,
        connection_factory: ConnectionFactory,
        enable_timing_metrics: bool = True,
    ) -> None:
        super().__init__(cache, hostname)
        self.ports = []               # type: List[str]
        self.connection_factory = connection_factory
        self.hostname = hostname
        self.lldp_info = {}           # type: Dict[str, LLDPRemoteInfo]
        self.lldp_time = 0.0          # time when LLDP info was last updated
        self.lldp_timeout = lldp_timeout
        self._lock = asyncio.Lock()   # Serialises port and lldp info
        self.enable_timing_metrics = enable_timing_metrics

    def __repr__(self) -> str:
        return 'Switch({!r})'.format(self.hostname)

    async def _run_command(self, command: str) -> str:
        result = await self.connection_factory.get_connection(self.hostname).run_process(command)
        if not isinstance(result, str):
            raise TypeError(f'Expected str, got {type(result)}')
        return result

    @staticmethod
    def _remove_welcome_messages(lines: List[str]) -> List[str]:
        if len(lines) > 0 and _LAST_LOGIN_RE.match(lines[0]):
            lines = lines[1:]
        if len(lines) > 0 and _TOTAL_CONNECTIONS_SINCE_RE.match(lines[0]):
            lines = lines[1:]
        return lines

    async def _populate_ports(self) -> None:
        """Populate the ports list"""
        if self.ports != []:  # ports are already populated
            return
        result = await self._run_command(r'show interfaces ethernet status')
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

    async def _update_lldp(self) -> None:
        logger.info('Updating LLDP information for %s', self.hostname)
        result = await self._run_command(
            r'show lldp interfaces ethernet remote '
            r'| include "^Eth|^ *Remote port description *:'
            r'|^ *Remote system name *:|^ *Remote port-id *:"'
        )
        port = None
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

        cmd = [f'show interfaces ethernet {port} counters'
               for port in self.ports]
        result = await self._run_command('\n'.join(cmd))
        cur_port = -1
        direction = None
        port = None
        info = dummy_info = LLDPRemoteInfo()
        lines = result.splitlines()
        lines = self._remove_welcome_messages(lines)

        for line in lines:
            line = line.strip()
            # MLNX-OS omits the colon, Onyx includes it
            if line in {'Rx', 'Rx:'}:
                cur_port += 1
                port = self.ports[cur_port]
                info = self.lldp_info.get(port, dummy_info)
            if line in {'Rx', 'Tx', 'Rx:', 'Tx:'}:
                direction = line[:2].lower()
            else:
                match = _COUNTER_RE.match(line)
                if match and match.group(2) in metrics.COUNTERS:
                    # To enable exact deltas, wrap every 2^53 so that
                    # there is no rounding in IEEE double precision.
                    count = int(match.group(1)) & (2**53 - 1)
                    name = match.group(2)
                    labels = (port, direction, info.name,
                              info.port_id, info.port_description)
                    interface_counters[name].labels(*labels).inc(count)
        assert cur_port == len(self.ports) - 1, f'cur_port: {cur_port}, ports: {len(self.ports)}'

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
        for line in result.splitlines():
            line = line.strip()
            if line.startswith('Eth'):
                fields = line.split()
                port = fields[0][3:]
                info = self.lldp_info.get(port, dummy_info)
                labels = (port, info.name, info.port_id, info.port_description)
                port_enabled.labels(*labels).set(int(fields[1] == 'Enabled'))
                port_up.labels(*labels).set(int(fields[2] == 'Up'))

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

        cmd = r'show interfaces ethernet | include "^\s+Last change in operational status: "'
        result = await self._run_command(cmd)
        # for some reason the output may have the first line as a welcome message even after
        # applying the include filter
        lines = self._remove_welcome_messages(result.splitlines())
        assert len(lines) == len(self.ports), f'lines: {len(lines)}, ports: {len(self.ports)}'
        cur_port = -1
        for line in lines:
            cur_port += 1
            port = self.ports[cur_port]
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            match = _OPERATIONAL_CHANGES_RE.match(line)
            if match:
                port_operational_changes.labels(*labels).inc(int(match.group(2)))
            else:
                if not _OPERATIONAL_CHANGES_NEVER_RE.match(line):
                    logger.warning('Unexpected line in show interfaces ethernet: %s', line)
                port_operational_changes.labels(*labels).inc(0)

    async def _scrape_link_diagnostic_code(
        self,
        registry: prometheus_client.CollectorRegistry
    ) -> None:
        _state_labelnames = ('port', 'remote_name', 'remote_port_id', 'remote_port_description')
        port_link_diagnostic_state = prometheus_client.Gauge(
            'switch_port_link_diagnostic_state', 'state of the link',
            labelnames=_state_labelnames,
            registry=registry
        )

        cmd = r'show interfaces ethernet link-diagnostics | include "^\s+Eth"'
        result = await self._run_command(cmd)
        lines = result.splitlines()
        # for some reasone the output may have the first line as a welcome message even after
        # applying the include filter
        lines = self._remove_welcome_messages(result.splitlines())

        cur_port = -1
        for line in lines:
            cur_port += 1
            if cur_port >= len(self.ports):
                logger.debug('cur_port: %s, ports: %s', cur_port, self.ports)
                logger.debug('line: %s', line)
                logger.debug('result: %s', result)
            port = self.ports[cur_port]
            line = line.strip()
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            match = _DIAGNOSTIC_CODE_RE.match(line)
            if match:
                port_link_diagnostic_state.labels(*labels).set(int(match.group(1)))
            else:
                logger.warning(
                    'Unexpected line in show interfaces ethernet link-diagnostics: %s', line)

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
        results = _TRANSCEIVER_POWER_SECTION_RE.split(result)
        # When using re.split() with capturing groups, the result alternates:
        # [text_before_first_match, captured_group_1, text_after_match_1, captured_group_2,
        # text_after_match_2, ...]
        # Skip the first element (text before any match), then iterate in pairs: (port, section)
        for i in range(1, len(results) - 1, 2):
            port = results[i]
            section = results[i + 1]
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)

            matches = _TRANSCEIVER_POWER_RX_RE.finditer(section)
            match = None  # type: Union[re.Match[str], None]
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

    async def timed(self, coroutine: Coroutine[Any, Any, None], timing_gauge: prometheus_client.Gauge) -> None:
        start_time = time.perf_counter()
        await coroutine
        end_time = time.perf_counter()
        duration = end_time - start_time
        if self.enable_timing_metrics:
            timing_gauge.labels(self.hostname, coroutine.__name__).set(duration)

    async def scrape(self, timeout: float) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()
        async with self._lock:
            await self._populate_ports()
            await self._update_lldp_periodically()

        registry = prometheus_client.CollectorRegistry()
        timing_gauge = prometheus_client.Gauge(
            'switch_coroutine_duration_seconds', 'duration of the coroutine',
            labelnames=('hostname', 'coroutine'),
            registry=registry,
        )

        # TODO: Use a TaskGroup instead of a list of tasks to robustly handle the async context.
        scrapers = [
            self._scrape_counters(registry),
            self._scrape_state(registry),
            self._scrape_operational_changes(registry),
            self._scrape_link_diagnostic_code(registry),
            self._scrape_transceiver_power(registry),
        ]
        tasks = [asyncio.create_task(self.timed(s, timing_gauge), name=s.__name__)
                 for s in scrapers]
        timeout = timeout - (time.perf_counter() - start_time)
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        exceptions = []
        for task in pending:
            logger.error('[%s] Cancelling scraping metrics: %s', self.hostname, task.get_name())
            task.cancel()
        for task in done:
            try:
                task.result()
            except Exception as e:
                exceptions.append(e)

        if exceptions:
            ex = Exception(
                "Error during scraping metrics: " + ', '.join([str(e) for e in exceptions])
            )
            logger.error(ex)
            raise ex
        return registry

    @override
    async def close(self) -> None:
        await self.connection_factory.get_connection(self.hostname).close()
