import logging
import asyncio
import re
import time
import traceback
from typing import Coroutine, Dict, List, Tuple
from typing_extensions import override       # noqa: F401

import attr
import asyncssh
import prometheus_client

from .cache import Cache, Item
from . import metrics

logger = logging.getLogger(__name__)

MAXIMUM_CONCURRENT_SSH_PROCESSES = 5
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
_TRANSCEIVER_POWER_TX_RE = re.compile(r'(\w+) Tx Power\s*: .* mW / ([-]?\d+\.\d+) dBm')
_TRANSCEIVER_POWER_RX_RE = re.compile(r'(\w+) Rx Power\s*: .* mW / ([-]?\d+\.\d+) dBm')
_TRANSCEIVER_POWER_HI_RX_THRESHOLD_RE = re.compile(
    r'\s*Hi Rx Power Alarm Thresh\s*: .* mW / ([-]?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_LOW_RX_THRESHOLD_RE = re.compile(
    r'\s*Low Rx Power Alarm Thresh\s*: .* mW / ([-]?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_HI_TX_THRESHOLD_RE = re.compile(
    r'\s*Hi Tx Power Alarm Thresh\s*: .* mW / ([-]?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_LOW_TX_THRESHOLD_RE = re.compile(
    r'\s*Low Tx Power Alarm Thresh\s*: .* mW / ([-]?\d+\.\d+) dBm'
)
_TRANSCEIVER_POWER_SECTION_RE = re.compile(r'Port (.*) transceiver diagnostic data:')


@attr.s(slots=True)
class LLDPRemoteInfo:
    name = attr.ib(type=str, default='')
    port_id = attr.ib(type=str, default='')
    port_description = attr.ib(type=str, default='')


class ProcessPool:
    """Pool of asyncssh SSHClientProcesses for running commands on the switch.
    
    The pool allows us to create the channels before the scraping starts.
    """
    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self.conn = conn
        self.process_stack = []       # type: List[asyncssh.SSHClientProcess[str]]
        self.semaphore = asyncio.Semaphore(5)
        self._lock = asyncio.Lock()   # Serialises refills

    async def run_process(self, command: str) -> str:
        """Get a process from the pool."""
        async with self.semaphore:
            if len(self.process_stack) == 0:
                await self.refill_stack()
            process = self.process_stack.pop(0)
            logger.debug('Running command %s', command)
            stdout, stderr = await process.communicate(command)
            process.close()
            if stderr:
                logger.error('[%s] Error running command %s: %s', self.hostname, command, stderr)
            return stdout

    def close(self) -> None:
        self.semaphore = asyncio.Semaphore(0)
        for process in self.process_stack:
            process.close()
        self.process_stack = []

    async def refill_stack(self) -> None:
        async with self._lock:
            for _ in range(max(0, MAXIMUM_CONCURRENT_SSH_PROCESSES - len(self.process_stack))):
                self.process_stack.append(await self.conn.create_process())


class Switch(Item):
    """Collect statistics about a single switch.

    An instance has an SSH connection that is initialised on first use. It
    does not automatically reconnect: if you get a connection error, throw
    it away (via :meth:`destroy`) and create a new one.
    """

    def __init__(self, cache: Cache, hostname: str, username: str,
                 password: str, keyfile: str, lldp_timeout: float) -> None:
        super().__init__(cache, hostname)
        self.conn = None
        self.ports = []               # type: List[str]
        self.process_pool = None      # type: ProcessPool
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.lldp_info = {}           # type: Dict[str, LLDPRemoteInfo]
        self.lldp_time = 0.0          # time when LLDP info was last updated
        self.lldp_timeout = lldp_timeout
        self._lock = asyncio.Lock()   # Serialises connect and update_lldp
        self.enable_timing_metrics = True
        self.registry = prometheus_client.CollectorRegistry()

        # All Prometheus metric objects are initialised up-front and then
        # populated/cleared on each scrape. This avoids duplicate registrations
        # and makes per-scrape code purely about setting values.
        self.timing_histogram = prometheus_client.Histogram(
            'switch_coroutine_duration_seconds', 'duration of the coroutine',
            labelnames=('hostname', 'coroutine'),
            registry=self.registry,
            buckets=list(range(1, 12))+[float('inf')]
        )

        self.interface_counters = {}
        for name in metrics.COUNTERS:
            metric = metrics.name_to_metric(name)
            self.interface_counters[name] = prometheus_client.Counter(
                metric, 'total number of ' + name,
                labelnames=('port', 'direction', 'remote_name',
                            'remote_port_id', 'remote_port_description'),
                registry=self.registry
            )

        _state_labelnames = ('port', 'remote_name', 'remote_port_id', 'remote_port_description')
        self.port_enabled = prometheus_client.Gauge(
            'switch_port_enabled', 'whether port is administratively enabled',
            labelnames=_state_labelnames,
            registry=self.registry,
        )
        self.port_up = prometheus_client.Gauge(
            'switch_port_up', 'whether port is currently up',
            labelnames=_state_labelnames,
            registry=self.registry,
        )
        self.port_operational_changes = prometheus_client.Counter(
            'switch_port_operational_changes_total', 'total number of operational changes',
            labelnames=_state_labelnames,
            registry=self.registry
        )
        self.port_link_diagnostic_state = prometheus_client.Gauge(
            'switch_port_link_diagnostic_state', 'state of the link',
            labelnames=_state_labelnames,
            registry=self.registry
        )

        self.port_transceiver_power = prometheus_client.Gauge(
            'switch_port_transceiver_power_dbm', 'power of the tx channel in decibel milliwatts',
            labelnames=(
                'port', 'remote_name', 'remote_port_id', 'remote_port_description',
                'channel', 'direction'
            ),
            registry=self.registry
        )
        self.port_transceiver_hi_power_alarm_threshold = prometheus_client.Gauge(
            'switch_port_transceiver_hi_power_alarm_threshold_dbm',
            'hi power alarm threshold in decibel milliwatts',
            labelnames=(
                'port', 'remote_name', 'remote_port_id', 'remote_port_description',
                'direction'
            ),
            registry=self.registry
        )
        self.port_transceiver_low_power_alarm_threshold = prometheus_client.Gauge(
            'switch_port_transceiver_low_power_alarm_threshold_dbm',
            'low power alarm threshold in decibel milliwatts',
            labelnames=(
                'port', 'remote_name', 'remote_port_id', 'remote_port_description',
                'direction'
            ),
            registry=self.registry
        )

    def __repr__(self) -> str:
        return 'Switch({!r})'.format(self.hostname)

    async def _run_command(self, command: str) -> str:
        return await self.process_pool.run_process(command)

    async def _connect(self) -> None:
        """Establish the SSH connection"""
        if self.conn:
            return
        await self._connect_unlocked()

    async def _connect_unlocked(self) -> None:
        self.conn = await asyncssh.connect(
            self.hostname, known_hosts=None,
            username=self.username, password=self.password,
            client_keys=self.keyfile
        )
        self.process_pool = ProcessPool(self.conn)
        await self.process_pool.refill_stack()
        result = await self._run_command('show interfaces ethernet status')
        self.ports = []
        for line in result.splitlines():
            fields = line.split()
            if not fields:
                continue
            match = _PORT_RE.match(fields[0])
            if match:
                self.ports.append(match.group(1))

    async def _update_lldp(self) -> None:
        """Ensure the LLDP information is up to date"""
        now = time.time()
        if now - self.lldp_time < self.lldp_timeout:
            return
        await self._update_lldp()

    async def _update_lldp(self) -> None:
        logger.info('Updating LLDP information for %s', self.hostname)
        result = await self._run_command(
            'show lldp interfaces ethernet remote '
            '| include "^Eth|^ *Remote port description *:'
            '|^ *Remote system name *:|^ *Remote port-id *:"'
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

    async def _scrape_counters(self, _registry: prometheus_client.CollectorRegistry) -> None:
        cmd = ['show interfaces ethernet {} counters'.format(port)
               for port in self.ports]
        result = await self._run_command('\n'.join(cmd))
        cur_port = -1
        direction = None
        port = None
        info = dummy_info = LLDPRemoteInfo()
        for line in result.splitlines():
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
                    self.interface_counters[name].labels(*labels).inc(count)

    async def _scrape_state(self, _registry: prometheus_client.CollectorRegistry) -> None:
        result = await self._run_command('show interfaces ethernet description')
        dummy_info = LLDPRemoteInfo()
        for line in result.splitlines():
            line = line.strip()
            if line.startswith('Eth'):
                fields = line.split()
                port = fields[0][3:]
                info = self.lldp_info.get(port, dummy_info)
                labels = (port, info.name, info.port_id, info.port_description)
                self.port_enabled.labels(*labels).set(int(fields[1] == 'Enabled'))
                self.port_up.labels(*labels).set(int(fields[2] == 'Up'))

    async def _scrape_operational_changes(
        self,
        _registry: prometheus_client.CollectorRegistry
    ) -> None:
        cmd = r'show interfaces ethernet | include "^\s+Last change in operational status: "'
        result = await self._run_command(cmd)
        cur_port = -1
        for line in result.splitlines():
            cur_port += 1
            port = self.ports[cur_port]
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            match = _OPERATIONAL_CHANGES_RE.match(line)
            if match:
                self.port_operational_changes.labels(*labels).inc(int(match.group(2)))
            else:
                if not _OPERATIONAL_CHANGES_NEVER_RE.match(line):
                    logger.warning('Unexpected line in show interfaces ethernet: %s', line)
                self.port_operational_changes.labels(*labels).inc(0)

    async def _scrape_link_diagnostic_code(
        self,
        _registry: prometheus_client.CollectorRegistry
    ) -> None:
        cmd = r'show interfaces ethernet link-diagnostics | include "^\s+Eth"'
        result = await self._run_command(cmd)
        cur_port = -1
        for line in result.splitlines():
            cur_port += 1
            port = self.ports[cur_port]
            line = line.strip()
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            match = _DIAGNOSTIC_CODE_RE.match(line)
            if match:
                self.port_link_diagnostic_state.labels(*labels).set(int(match.group(1)))
            else:
                logger.warning('Unexpected line in show interfaces ethernet: %s', line)

    async def _scrape_transceiver_power(
        self,
        _registry: prometheus_client.CollectorRegistry
    ) -> None:
        result = await self._run_command(
            r'enable\nshow interfaces ethernet transceiver diagnostics'
        )
        results = _TRANSCEIVER_POWER_SECTION_RE.split(result)
        # When using re.split() with capturing groups, the result alternates:
        # [text_before_first_match, captured_group_1, text_after_match_1, captured_group_2,
        # text_after_match_2, ...]
        # Skip the first element (text before any match), then iterate in pairs: (port, section)
        for i in range(1, len(results), 2):
            if i + 1 >= len(results):
                break
            port = results[i]
            section = results[i + 1]
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)

            matches = _TRANSCEIVER_POWER_RX_RE.finditer(section)
            for match in matches:
                child = self.port_transceiver_power.labels(*labels, match.group(1), 'rx')
                child.set(float(match.group(2)))

            matches = _TRANSCEIVER_POWER_TX_RE.finditer(section)
            for match in matches:
                child = self.port_transceiver_power.labels(*labels, match.group(1), 'tx')
                child.set(float(match.group(2)))

            matches = _TRANSCEIVER_POWER_HI_RX_THRESHOLD_RE.findall(section)
            if matches:
                child = self.port_transceiver_hi_power_alarm_threshold.labels(*labels, 'rx')
                child.set(float(matches[0]))

            matches = _TRANSCEIVER_POWER_LOW_RX_THRESHOLD_RE.findall(section)
            if matches:
                child = self.port_transceiver_low_power_alarm_threshold.labels(*labels, 'rx')
                child.set(float(matches[0]))

            matches = _TRANSCEIVER_POWER_HI_TX_THRESHOLD_RE.findall(section)
            if matches:
                child = self.port_transceiver_hi_power_alarm_threshold.labels(*labels, 'tx')
                child.set(float(matches[0]))

            matches = _TRANSCEIVER_POWER_LOW_TX_THRESHOLD_RE.findall(section)
            if matches:
                child = self.port_transceiver_low_power_alarm_threshold.labels(*labels, 'tx')
                child.set(float(matches[0]))

    async def timed(self, coroutine: Coroutine) -> Coroutine:
        
        start_time = time.perf_counter()
        result = await coroutine
        end_time = time.perf_counter()
        duration = end_time - start_time
        if self.enable_timing_metrics:
            self.timing_histogram.labels(self.hostname, coroutine.__name__).observe(duration)
        return result

    async def scrape(self, timeout: float) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        start_time = time.perf_counter()
        await self._connect()
        await self._update_lldp()

        # Clear scrape-populated metrics to avoid stale series and to ensure
        # Counters reflect the current absolute value (we re-add samples below).
        for counter in self.interface_counters.values():
            counter.clear()
        self.port_enabled.clear()
        self.port_up.clear()
        self.port_operational_changes.clear()
        self.port_link_diagnostic_state.clear()
        self.port_transceiver_power.clear()
        self.port_transceiver_hi_power_alarm_threshold.clear()
        self.port_transceiver_low_power_alarm_threshold.clear()

        scrapers = [
            self._scrape_counters(self.registry),
            self._scrape_state(self.registry),
            self._scrape_operational_changes(self.registry),
            self._scrape_link_diagnostic_code(self.registry),
            self._scrape_transceiver_power(self.registry),
        ]
        tasks = [asyncio.create_task(self.timed(s), name=s.__name__) for s in scrapers]
        done, pending = await asyncio.wait(tasks, timeout=timeout - (time.perf_counter() - start_time))
        for task in pending:
            logger.error('[%s] Cancelling scraping metrics: %s', self.hostname, task.get_name())
            task.cancel()
        for task in done:
            try:
                task.result()
            except Exception as e:
                logger.error('[%s] Error scraping metrics: %s', self.hostname, e)
                logger.error('[%s] Traceback: %s', self.hostname, traceback.format_exc())
                raise

        return self.registry

    @override
    async def close(self) -> None:
        if self.process_pool:
            self.process_pool.close()
            self.process_pool = None
        if self.conn:
            self.conn.close()
            await self.conn.wait_closed()
            self.conn = None
