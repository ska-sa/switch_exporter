import asyncio
import functools
import logging
from typing import Callable, Dict, Union

import asyncssh
logger = logging.getLogger(__name__)


class Connection:
    def __init__(self, hostname: str, username: str, password: str, keyfile: str, on_close: Callable[[], None]) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.conn = None  # type: Union[asyncssh.SSHClientConnection, None]
        self._lock = asyncio.Lock()
        self.on_close = on_close

    async def run_process(self, command: str) -> Union[bytes, str, None]:
        """Get a process from the connection."""
        async with self._lock:
            if self.conn is None:
                self.conn = await asyncssh.connect(
                    self.hostname, username=self.username, password=self.password, client_keys=self.keyfile, known_hosts=None
                )

            logger.debug('Running command %s', command)
        completed_process = await self.conn.run(command=None, input=command)
        if completed_process.returncode != 0:
            logger.error(
                '[%s] Error running command %s: return code %s',
                self.hostname, command, completed_process.returncode
            )
            logger.debug('[%s] Stdout: %s', self.hostname, completed_process.stdout)
            logger.debug('[%s] Stderr: %s', self.hostname, completed_process.stderr)
        return completed_process.stdout

    async def close(self) -> None:
        async with self._lock:
            if self.conn is not None:
                self.conn.close()
                await self.conn.wait_closed()
                self.conn = None
                self.on_close()


class ConnectionFactory:
    def __init__(self, username: str, password: str, keyfile: str) -> None:
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.connections = {}  # type: Dict[str, Connection]

    def get_connection(self, hostname: str) -> Connection:
        if hostname not in self.connections:
            self.connections[hostname] = Connection(
                hostname, self.username, self.password, self.keyfile, functools.partial(self.close, hostname))
        return self.connections[hostname]

    def close(self, hostname: str) -> None:
        self.connections.pop(hostname)
