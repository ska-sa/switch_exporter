import asyncio
import logging

import asyncssh
logger = logging.getLogger(__name__)


MAXIMUM_CONCURRENT_SSH_PROCESSES = 5


class Connection:
    def __init__(self, hostname: str, username: str, password: str, keyfile: str) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.conn = None
        self._lock = asyncio.Lock()

    async def run_process(self, command: str) -> str:
        """Get a process from the connection."""
        async with self._lock:
            if self.conn is None:
                self.conn = await asyncssh.connect(
                    self.hostname, known_hosts=None,
                    username=self.username, password=self.password,
                    client_keys=self.keyfile
                )

        process = await self.conn.create_process()
        logger.debug('Running command %s', command)
        stdout, stderr = await process.communicate(command)
        process.close()
        if stderr:
            logger.error('[%s] Error running command %s: %s', self.hostname, command, stderr)
        return stdout

    def close(self) -> None:
        self.conn.close()
        self.conn = None


class ConnectionFactory:
    def __init__(self, username: str, password: str, keyfile: str) -> None:
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.connections = {}

    def get_connection(self, hostname: str) -> Connection:
        if hostname not in self.connections:
            self.connections[hostname] = Connection(
                hostname, self.username, self.password, self.keyfile)
        return self.connections[hostname]

    def close(self) -> None:
        for connections in self.connections.values():
            connections.close()
        self.connections = {}
