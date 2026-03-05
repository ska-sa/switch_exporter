import asyncio
import logging
from typing import List

import asyncssh
logger = logging.getLogger(__name__)


MAXIMUM_CONCURRENT_SSH_PROCESSES = 5


class ConnectionPool:
    """Pool of asyncssh SSHClientProcesses for running commands on the switch.

    The pool allows us to create the channels before the scraping starts.
    """

    def __init__(self, hostname: str, username: str, password: str, keyfile: str) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.conn = None
        self.process_stack = []       # type: List[asyncssh.SSHClientProcess[str]]
        self._lock = asyncio.Lock()   # Serialises refills

    async def run_process(self, command: str) -> str:
        """Get a process from the pool."""
        async with self._lock:
            if self.conn is None:
                self.conn = await asyncssh.connect(
                    self.hostname, known_hosts=None,
                    username=self.username, password=self.password,
                    client_keys=self.keyfile
                )
                await self.refill()

        async with self._lock:
            if len(self.process_stack) == 0: # the stack is empty, refill the stack
                await self.refill()
            process = self.process_stack.pop()
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
        self.conn.close()
        self.conn = None

    async def refill(self) -> None:
        for _ in range(max(0, MAXIMUM_CONCURRENT_SSH_PROCESSES - len(self.process_stack))):
            self.process_stack.append(await self.conn.create_process())

class ConnectionPoolFactory:
    def __init__(self, username: str, password: str, keyfile: str) -> None:
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.pools = {}

    def get_pool(self, hostname: str) -> ConnectionPool:
        if hostname not in self.pools:
            self.pools[hostname] = ConnectionPool(hostname, self.username, self.password, self.keyfile)
        return self.pools[hostname]

    def close(self) -> None:
        for pool in self.pools.values():
            pool.close()
        self.pools = {}