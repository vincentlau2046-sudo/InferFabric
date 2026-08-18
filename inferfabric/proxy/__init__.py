"""
inferfabric/proxy/ — Auto-routing proxy + web dashboard.

Subpackages:
  handler.py       — ProxyHandler (core HTTP handler, routing, forwarder)
  chat_handlers.py — _handle_chat / _handle_chat_ollama_native
  metrics.py        — VllmMetricsCollector (Prometheus parsing, EMA throughput)
"""

# Lazy imports to break circular dependency:
# proxy_manager → proxy.auth → proxy/__init__ → handler → proxy_manager
# By not importing handler at module level, we break the cycle.

def _get_handler():
    from .handler import ProxyHandler, ThreadedHTTPServer, main
    return ProxyHandler, ThreadedHTTPServer, main


def __getattr__(name):
    if name in ("ProxyHandler", "ThreadedHTTPServer", "main"):
        from .handler import ProxyHandler, ThreadedHTTPServer, main
        return locals()[name]
    raise AttributeError(f"module 'inferfabric.proxy' has no attribute '{name}'")


__all__ = ["ProxyHandler", "ThreadedHTTPServer", "main"]