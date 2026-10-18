"""Outbound HTTPS helper.

httpx only trusts its bundled certifi CAs, so a TLS-inspecting corporate proxy
(Zscaler and the like), whose CA lives in the OS certificate store, breaks every
call with CERTIFICATE_VERIFY_FAILED. This context trusts the OS store instead:
via truststore when installed (live lookups), else Python's default context,
which also loads the Windows/macOS system store. Verification stays on."""
from __future__ import annotations

import ssl
from functools import lru_cache


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()
