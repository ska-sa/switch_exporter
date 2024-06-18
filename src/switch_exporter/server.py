"""Web server with Prometheus metrics endpoint for counters"""
import asyncio
import functools
import argparse
import logging

import katsdpservices
from aiohttp import web
import async_timeout
import prometheus_client

from .switch import Switch
from .cache import Cache


#: Time to keep SSH connections open
CONNECTION_TIMEOUT = 120
SCRAPE_TIMEOUT = 5


async def get_metrics(request: web.Request) -> web.Response:
    try:
        target = request.query['target']
    except KeyError:
        raise web.HTTPBadRequest(text='target parameter omitted')
    cache = request.app['cache']
    switch = cache.get(target)
    timeout = request.app['scrape_timeout']
    try:
        with async_timeout.timeout(timeout), switch:
            counters = await switch.scrape()
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        raise web.HTTPGatewayTimeout(
            text='Scrape timed out after {}s'.format(timeout))
    except Exception as exc:
        # Possibly a failed connection, so reset it
        switch.destroy()
        raise web.HTTPInternalServerError(text='Scrape failed: ' + str(exc))
    else:
        content = prometheus_client.generate_latest(counters).decode()
        return web.Response(text=content)


def make_app(args: argparse.Namespace) -> web.Application:
    app = web.Application()
    factory = functools.partial(
        Switch,
        lldp_timeout=args.lldp_timeout,
        username=args.username,
        password=args.password)
    app['cache'] = Cache(factory, args.connection_timeout)
    app['scrape_timeout'] = args.scrape_timeout
    app.router.add_get('/metrics', get_metrics)
    return app


def get_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--username', default='monitor',
        help='Username on switches')
    parser.add_argument(
        '--password', default='monitor',
        help='Password on switches')
    parser.add_argument(
        '--keyfile',
        help='SSH key for switches')
    parser.add_argument(
        '--connection-timeout', type=float, default=120.0, metavar='SECONDS',
        help='Time to cache open SSH connections [%(default)s]')
    parser.add_argument(
        '--scrape-timeout', type=float, default=8.0, metavar='SECONDS',
        help='Timeout to obtain data from a switch [%(default)s]')
    parser.add_argument(
        '--lldp-timeout', type=float, default=300.0, metavar='SECONDS',
        help='Interval between refreshing LLDP information [%(default)s]')
    parser.add_argument(
        '--port', type=int, default=9116, help='Web server port number')
    parser.add_argument(
        '--bind', help='Web server local address')
    parser.add_argument(
        '--log-level', default='INFO', help='Log level [%(default)s]')
    return parser.parse_args()


def main() -> None:
    args = get_arguments()
    katsdpservices.setup_logging()
    logging.root.setLevel(args.log_level.upper())
    app = make_app(args)
    web.run_app(app, host=args.bind, port=args.port)


if __name__ == '__main__':
    main()
