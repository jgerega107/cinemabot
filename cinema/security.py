"""Bounded, in-memory admission control and authenticated response caching."""
import asyncio
import ipaddress
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

from cinema.errors import Problem


async def reject_brotli(response):
    # Runs before HTTPX reads/decodes a response, even if an optional Brotli package is installed.
    if "br" in {x.strip().lower() for x in response.headers.get("content-encoding", "").split(",")}:
        raise Problem(502, "The upstream server returned an unsupported content encoding.")


class Limits:
    def __init__(self, max_keys=4096):
        self.buckets = OrderedDict()
        self.max_keys = max_keys
        self.active = {}

    def rate(self, key, *, per_minute, burst):
        now = time.monotonic()
        tokens, updated = self.buckets.pop(key, (float(burst), now))
        tokens = min(burst, tokens + max(0, now - updated) * per_minute / 60)
        self.buckets[key] = (tokens, now)
        if len(self.buckets) > self.max_keys:
            self.buckets.popitem(last=False)
        if tokens < 1:
            raise Problem(429, "Too many requests. Wait a moment and try again.")
        self.buckets[key] = (tokens - 1, now)

    @asynccontextmanager
    async def slot(self, group, key, *, total, individual):
        shared, personal = (group, None), (group, key)
        if self.active.get(shared, 0) >= total or self.active.get(personal, 0) >= individual:
            raise Problem(429, "Too many requests in progress. Try again shortly.")
        self.active[shared] = self.active.get(shared, 0) + 1
        self.active[personal] = self.active.get(personal, 0) + 1
        try:
            yield
        finally:
            for item in (shared, personal):
                self.active[item] -= 1
                if not self.active[item]:
                    del self.active[item]


def client_address(request, trusted_networks):
    """Trust forwarded addresses only from explicitly configured proxy peers."""
    peer = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(peer)
        if any(address in net for net in trusted_networks):
            header = request.headers.get("x-forwarded-for", "")
            if len(header) <= 1024 and len(header.split(",")) <= 16:
                chain = [ipaddress.ip_address(part.strip()) for part in header.split(",") if part.strip()]
                for candidate in reversed(chain):
                    if not any(address in net for net in trusted_networks):
                        break
                    address = candidate
        # Rotating interface IDs within one IPv6 allocation must not reset the budget.
        return str(ipaddress.ip_network(f"{address}/64", strict=False)) if address.version == 6 else str(address)
    except ValueError:
        return peer


class ResponseCache:
    def __init__(self, *, max_bytes, ttl, size, max_entries=256):
        self.max_bytes, self.ttl, self.size, self.max_entries = max_bytes, ttl, size, max_entries
        self.entries = OrderedDict()
        self.pending = {}
        self.bytes = 0

    async def get(self, key, fetch):
        now = time.monotonic()
        for expired in [k for k, (until, _, _) in self.entries.items() if until <= now]:
            self.bytes -= self.entries.pop(expired)[2]
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key][1]
        if key not in self.pending:
            if len(self.pending) >= 16:
                raise Problem(503, "Catalog requests are busy. Try again shortly.")
            self.pending[key] = asyncio.create_task(self._fetch(key, fetch))
            self.pending[key].add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return await asyncio.shield(self.pending[key])

    async def _fetch(self, key, fetch):
        try:
            value = await fetch()
            size = self.size(value)
            if size <= self.max_bytes:
                while self.entries and (self.bytes + size > self.max_bytes or len(self.entries) >= self.max_entries):
                    self.bytes -= self.entries.popitem(last=False)[1][2]
                self.entries[key] = (time.monotonic() + self.ttl, value, size)
                self.bytes += size
            return value
        finally:
            self.pending.pop(key, None)

    async def close(self):
        pending = list(self.pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.entries.clear()
        self.bytes = 0
