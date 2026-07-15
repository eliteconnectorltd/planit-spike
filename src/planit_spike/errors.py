"""Exception formatting shared by the fetch layers.

httpx sometimes raises with an empty message — a bare ConnectError whose str()
is "" tells a live-run reader nothing. The real cause is usually one link down
the chain (an OSError like [Errno 11001] getaddrinfo failed), so walk
__cause__/__context__ before falling back to repr.

Leaf module: stdlib only, no in-package imports.
"""

from __future__ import annotations


def format_exception(e: BaseException) -> str:
    """Describe an exception, never returning a bare 'ClassName: ' with no detail.

    Falls back through __cause__, then __context__, then repr, so an httpx
    error with an empty message still names the underlying socket failure.
    """
    text = str(e).strip()
    if not text:
        underlying = e.__cause__ or e.__context__
        if underlying is not None:
            inner = str(underlying).strip() or repr(underlying)
            text = f"caused by {type(underlying).__name__}: {inner}"
        else:
            text = repr(e)
    return f"{type(e).__name__}: {text}"
