"""
inferfabric/proxy.py — Re-export stub for backward compatibility.

`from inferfabric.proxy import ProxyHandler` still works because the
`proxy/` subpackage has the same module-level exports.
"""

from .proxy import ProxyHandler, ThreadedHTTPServer, main

__all__ = ["ProxyHandler", "ThreadedHTTPServer", "main"]