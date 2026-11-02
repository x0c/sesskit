"""Shared frozen JSONL feed for historical performance adapters."""

from sesskit.native_jsonl import NativeJsonlRows as NativePerformanceRows
from sesskit.native_jsonl import read_jsonl_snapshot

__all__ = ["NativePerformanceRows", "read_jsonl_snapshot"]
