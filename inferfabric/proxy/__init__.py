"""
inferfabric/proxy/ — Auto-routing proxy + web dashboard (v4.1 P3 split).

Subpackages:
  handler.py       — ProxyHandler (core HTTP handler, routing, forwarder)
  chat_handlers.py — _handle_chat / _handle_chat_ollama_native
  metrics.py       — VllmMetricsCollector (Prometheus parsing, EMA throughput)
"""

from .handler import ProxyHandler, ThreadedHTTPServer, main

__all__ = ["ProxyHandler", "ThreadedHTTPServer", "main"]