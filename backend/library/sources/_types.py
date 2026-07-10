"""Shared dependency types for library data sources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx


HttpGet = Callable[..., Awaitable[httpx.Response]]
Clock = Callable[[], float]


__all__ = ['Clock', 'HttpGet']
