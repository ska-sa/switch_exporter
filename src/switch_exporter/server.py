"""Web server with Prometheus metrics endpoint for counters"""
import asyncio
import functools
import argparse
import logging
from typing import Callable

import katsdpservices
from aiohttp import web
import prometheus_client

from .scraper import Scraper, ValidationError
from .switch import Switch
from .cache import Cache


#: Time to keep SSH connections open
CONNECTION_TIMEOUT = 120
logger = logging.getLogger(__name__)


async def get_metrics(request: web.Request) -> web.Response:
    try:
        target = request.query['target']
    except KeyError:
        raise web.HTTPBadRequest(text='target parameter omitted')

    collect = request.query.getall('collect', None)
    cache = request.app['cache']
    scraper = cache.get(target)
    timeout = request.app['scrape_timeout']
    try:
        timeout = int(request.query.get('scrape_timeout', timeout))
    except ValueError:
        logger.exception('Invalid scrape_timeout value')
        raise web.HTTPBadRequest(text='scrape_timeout must be an integer') from None
    try:
        with scraper:
            counters = await scraper.scrape(timeout, collect)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.exception('Scrape timed out')
        raise web.HTTPGatewayTimeout(
            text='Scrape timed out after {}s'.format(timeout)
        ) from None
    except ValidationError as e:
        logger.exception('Validation error during scrape')
        raise web.HTTPBadRequest(text=str(e)) from None
    except Exception as exc:
        # Possibly a failed connection, so reset it
        logger.exception('Exception during scrape, resetting switch')
        scraper.destroy()
        raise web.HTTPInternalServerError(text='Scrape failed: ' + str(exc)) from None
    else:
        content = prometheus_client.generate_latest(counters).decode()
        return web.Response(text=content)


def scraper_factory(switch_factory) -> Callable[[Cache, str], Scraper]:
    def scraper(cache: Cache, target: str) -> Scraper:
        switch = switch_factory(target)
        return Scraper(cache, switch)
    return scraper


async def make_app(args: argparse.Namespace, loop: asyncio.AbstractEventLoop) -> web.Application:
    app = web.Application(loop=loop)
    switch_factory = functools.partial(
        Switch,
        username=args.username,
        password=args.password,
        keyfile=args.keyfile,
        lldp_timeout=args.lldp_timeout,
    )
    app['cache'] = Cache(scraper_factory(switch_factory), args.connection_timeout)
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
        '--keyfile', default=(),
        help='SSH client key for switches')
    parser.add_argument(
        '--connection-timeout', type=float, default=120.0, metavar='SECONDS',
        help='Time to cache open SSH connections [%(default)s]')
    parser.add_argument(
        '--scrape-timeout', type=float, default=10.0, metavar='SECONDS',
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

    katsdpservices.add_aiomonitor_arguments(parser)
    return parser.parse_args()


def main() -> None:
    args = get_arguments()
    katsdpservices.setup_logging()
    logging.root.setLevel(args.log_level.upper())
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(make_app(args, loop))

    with katsdpservices.aiomonitor.start_aiomonitor(loop, args=args, locals=locals()):
        web.run_app(app, host=args.bind, port=args.port, loop=loop)


if __name__ == '__main__':
    main()
