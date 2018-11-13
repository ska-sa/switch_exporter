import asyncio
import logging
from typing import TypeVar, Generic, Dict, Set, Callable     # noqa: F401


logger = logging.getLogger(__name__)
_T = TypeVar('_T')


def log_exception(future: asyncio.Future) -> None:
    """Log any exception from a background task"""
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception('Exception in cleanup task')


class Item(Generic[_T]):
    """Base class for cache items.

    This should be overloaded to implement at least a constructor and
    :meth:`close`. It should not be constructed directly; use
    :meth:`.Cache.get` instead.
    """
    def __init__(self, cache: 'Cache', key: _T) -> None:
        self._cache = cache
        self._users = 0
        self._timeout_handle = None
        self._destroying = False
        self._cache_key = key
        self._timeout_handle = asyncio.get_event_loop().call_later(
            self._cache.timeout, self.destroy)
        # Invariants:
        # - _destroying is true iff self has been removed from the cache
        # - _timeout_handle is set iff _destroying is false and users is 0
        # - _destroy has been called iff _destroying is true and users is 0

    def _cancel_timeout(self) -> None:
        if self._timeout_handle:
            self._timeout_handle.cancel()
            self._timeout_handle = None

    def __enter__(self) -> 'Item[_T]':
        assert not self._destroying
        self._users += 1
        self._cancel_timeout()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._users -= 1
        if self._users == 0:
            if self._destroying:
                self._destroy()
            else:
                self._timeout_handle = asyncio.get_event_loop().call_later(
                    self._cache.timeout, self.destroy)
        return False    # Allow exceptions to propagate

    def _destroy(self) -> None:
        assert self._destroying and self._users == 0
        logging.info('Closing down %r', self)
        task = asyncio.get_event_loop().create_task(self.close())
        self._cache._add_cleanup(task)

    def destroy(self) -> None:
        """Remove from the cache, and close once no more users.

        This may be called explicitly by the user. It is idempotent.
        """
        if self._destroying:
            return
        self._cache._items.pop(self._cache_key, None)
        self._destroying = True
        self._cancel_timeout()
        if self._users == 0:
            self._destroy()

    async def close(self) -> None:
        """Release any resources associated with the item.

        This is a hook for subclasses to implement.
        """
        pass


class Cache(Generic[_T]):
    """Cache where items are removed when not used for some length of time.

    To use, subclass :class:`Item`. After retrieving an item with
    :meth:`get`, immediately use it as a context manager. It will remain live
    at least as long as the context manager lives, plus a timeout.

    Parameters
    ----------
    item_cls : class
        Class for items, which should derive from :class:`Item`. May
        also be a factory function.
    timeout : float
        Timeout for purging items from the cache.
    """

    def __init__(self, item_cls: Callable[['Cache[_T]', _T], Item[_T]],
                 timeout: float) -> None:
        self._items = {}             # type: Dict[_T, Item[_T]]
        self._cleanups = set()       # type: Set[asyncio.Future]
        self.item_cls = item_cls
        self.timeout = timeout

    def get(self, key: _T) -> Item[_T]:
        """Obtain an item from the cache, creating it if necessary."""
        try:
            item = self._items[key]
        except KeyError:
            item = self.item_cls(self, key)
            logging.info('Created %r', item)
            self._items[key] = item
        return item

    def _add_cleanup(self, task: asyncio.Future) -> None:
        self._cleanups.add(task)
        task.add_done_callback(log_exception)
        task.add_done_callback(self._cleanups.discard)

    async def close(self) -> None:
        """Remove all items from the cache and wait for them to clean up"""
        while self._items:
            _, item = self._items.popitem()
            item.destroy()
        while self._cleanups:
            await next(iter(self._cleanups))
