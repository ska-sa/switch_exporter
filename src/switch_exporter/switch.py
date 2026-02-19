import logging
import asyncio
import re
import time
from typing import Dict, List       # noqa: F401

import attr
import asyncssh
import prometheus_client

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
    re.compile(r'Last change in operational status: (.*) \((\d+) oper change\)')
_OPERATIONAL_CHANGES_NEVER_RE = re.compile(r'Last change in operational status: Never')
_DIAGNOSTIC_CODE_RE = re.compile(r'^Eth\d+\/\d+\s+(\d+)')


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
    def __init__(self, cache: Cache, hostname: str, username: str,
                 password: str, keyfile: str, lldp_timeout: float) -> None:
        super().__init__(cache, hostname)
        self.conn = None
        self.ports = []               # type: List[str]
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.lldp_info = {}           # type: Dict[str, LLDPRemoteInfo]
        self.lldp_time = 0.0          # time when LLDP info was last updated
        self.lldp_timeout = lldp_timeout
        self._lock = asyncio.Lock()   # Serialises connect and update_lldp

    def __repr__(self) -> str:
        return 'Switch({!r})'.format(self.hostname)

    async def _run_command(self, command: str) -> str:
        assert self.conn is not None
        logger.debug('Sending command %s', command)
        result = await self.conn.run(command=None, input=command)
        logger.debug('Received response %s', result)
        return result.stdout

    async def _connect(self) -> None:
        """Establish the SSH connection"""
        if self.conn:
            return
        async with self._lock:
            await self._connect_unlocked()

    async def _connect_unlocked(self) -> None:
        self.conn = await asyncssh.connect(
            self.hostname, known_hosts=None,
            username=self.username, password=self.password,
            client_keys=self.keyfile)
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
        async with self._lock:
            await self._update_lldp_unlocked()

    async def _update_lldp_unlocked(self) -> None:
        logger.info('Updating LLDP information for %s', self.hostname)
        result = await self._run_command(
            'show lldp interfaces ethernet remote '
            '| include "^Eth|^ *Remote port description *:'
            '|^ *Remote system name *:|^ *Remote port-id *:"')
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
        self.lldp_time = time.time()

    async def _scrape_counters(self, registry: prometheus_client.CollectorRegistry) -> None:
        cmd = ['show interfaces ethernet {} counters'.format(port)
               for port in self.ports]
        result = await self._run_command('\n'.join(cmd))
        cur_port = -1
        direction = None
        port = None
        counters = {}
        for name in metrics.COUNTERS:
            metric = metrics.name_to_metric(name)
            counters[name] = prometheus_client.Counter(
                metric, 'total number of ' + name,
                labelnames=('port', 'direction', 'remote_name',
                            'remote_port_id', 'remote_port_description'),
                registry=registry)
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
                    counters[name].labels(*labels).inc(count)

    async def _scrape_state(self, registry: prometheus_client.CollectorRegistry) -> None:
        result = await self._run_command('show interfaces ethernet description')
        dummy_info = LLDPRemoteInfo()
        labelnames = ('port', 'remote_name', 'remote_port_id', 'remote_port_description')
        enabled = prometheus_client.Gauge(
            'switch_port_enabled', 'whether port is administratively enabled',
            labelnames=labelnames,
            registry=registry,
        )
        up = prometheus_client.Gauge(
            'switch_port_up', 'whether port is currently up',
            labelnames=labelnames,
            registry=registry,
        )
        for line in result.splitlines():
            line = line.strip()
            if line.startswith('Eth'):
                fields = line.split()
                port = fields[0][3:]
                info = self.lldp_info.get(port, dummy_info)
                labels = (port, info.name, info.port_id, info.port_description)
                enabled.labels(*labels).set(int(fields[1] == 'Enabled'))
                up.labels(*labels).set(int(fields[2] == 'Up'))

    async def _scrape_operational_changes(
        self,
        registry: prometheus_client.CollectorRegistry
    ) -> None:
        cmd = 'show interfaces ethernet | include "^\\s+Last change"'
        result = await self._run_command(cmd)
        cur_port = -1
        counter = prometheus_client.Counter(
            'switch_port_operational_changes_total', 'total number of operational changes',
            labelnames=('port', 'remote_name', 'remote_port_id', 'remote_port_description'),
            registry=registry)
        for line in result.splitlines():
            cur_port += 1
            port = self.ports[cur_port]
            line = line.strip()
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            match = _OPERATIONAL_CHANGES_RE.fullmatch(line)
            if match:
                last_change_total = int(match.group(2))
                counter.labels(*labels).inc(last_change_total)
            else:
                if _OPERATIONAL_CHANGES_NEVER_RE.fullmatch(line):
                    counter.labels(*labels).inc(0)
                else:
                    logger.warning('Unexpected line in show interfaces ethernet: %s', line)

    _DIAGNOSTIC_CODE_TO_STATE = {
        0: 'ok',
        1024: 'unplugged',
    }

    async def _scrape_diagnostic_code(
        self,
        registry: prometheus_client.CollectorRegistry
    ) -> None:
        cmd = 'show interfaces ethernet link-diagnostics | include "^\\s+Eth"'
        result = await self._run_command(cmd)
        cur_port = -1
        metric = prometheus_client.Enum(
            'switch_port_link_diagnostic_state', 'state of the link',
            labelnames=('port', 'remote_name', 'remote_port_id', 'remote_port_description'),
            registry=registry, states=['ok', 'unplugged', 'unknown'])
        for line in result.splitlines():
            cur_port += 1
            port = self.ports[cur_port]
            line = line.strip()
            info = self.lldp_info.get(port, LLDPRemoteInfo())
            labels = (port, info.name, info.port_id, info.port_description)
            match = _DIAGNOSTIC_CODE_RE.match(line)
            if match:
                state = self._DIAGNOSTIC_CODE_TO_STATE.get(int(match.group(1)), 'unknown')
                metric.labels(*labels).state(state)
                if state == 'unknown':
                    logger.warning('Unknown diagnostic code: %s', match.group(1))
            else:
                logger.warning('Unexpected line in show interfaces ethernet: %s', line)

    async def scrape(self) -> prometheus_client.CollectorRegistry:
        """Obtain the metrics from the switch"""
        await self._connect()
        await self._update_lldp()
        registry = prometheus_client.CollectorRegistry()

        await asyncio.gather(
            self._scrape_counters(registry),
            self._scrape_state(registry),
            self._scrape_operational_changes(registry),
            self._scrape_diagnostic_code(registry)
        )
        return registry

    async def close(self) -> None:
        if self.conn:
            self.conn.close()
            await self.conn.wait_closed()
            self.conn = None
