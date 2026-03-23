import logging
from typing import Union

import asyncssh
logger = logging.getLogger(__name__)


class Connection:
    def __init__(self, hostname: str, username: str, password: str, keyfile: str) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.keyfile = keyfile
        self.conn = None  # type: Union[asyncssh.SSHClientConnection, None]

    async def run_process(self, command: str) -> Union[bytes, str, None]:
        """Get a process from the connection."""
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
        if self.conn is not None:
            self.conn.close()
            await self.conn.wait_closed()
            self.conn = None
