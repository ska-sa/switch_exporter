import asyncio
import logging
from typing import Union

import asyncssh
logger = logging.getLogger(__name__)


class Connection:
    """A wrapper around an SSH connection to a switch.

    The connection is created on first use and cached untill method :meth:`close` is called.
    """

    def __init__(self, hostname: str, username: str, password: str, keyfile: str) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.conn: Union[asyncssh.SSHClientConnection, None] = None
        self.connection = asyncio.Lock()

    async def run_process(self, command: str) -> str:
        """Run a command and return the output, create a connection if it doesn't exist.

        This function is reentrant, so it can be called from multiple coroutines.
        """
        async with self.connection:
            if self.conn is None:
                self.conn = await asyncssh.connect(
                    self.hostname,
                    username=self.username,
                    password=self.password,
                    client_keys=self.keyfile,
                    known_hosts=None
                )

        logger.debug('[%s] Running command %r', self.hostname, command)

        completed_process = await self.conn.run(command=None, input=command)
        if completed_process.returncode != 0:
            logger.error(
                '[%s] Error running command %s: return code %s',
                self.hostname, command, completed_process.returncode
            )
            logger.debug('[%s] Stdout: %r', self.hostname, completed_process.stdout)
            logger.debug('[%s] Stderr: %r', self.hostname, completed_process.stderr)
        if not isinstance(completed_process.stdout, str):
            raise TypeError(f'Expected str, got {type(completed_process.stdout)}')
        return completed_process.stdout

    async def close(self) -> None:
        """Close the connection.

        This method is called by the cache when the item is removed from the cache.
        It relies on external methods to prevent race conditions.
        """
        if self.conn is not None:
            self.conn.close()
            await self.conn.wait_closed()
            self.conn = None
