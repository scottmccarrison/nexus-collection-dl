"""Client-safe error text for the web UI.

Exception messages can carry filesystem paths and reflected API request
detail. The web UI is the one boundary that does not go through the
CLI's redaction layer, so HTTP and SSE responses never carry raw
exception text: callers get a category only, and the detail stays on
the server side where it belongs.
"""

from ..api import NexusAPIError
from ..collection import CollectionParseError, ModParseError
from ..state import StateBusyError, StateError


def client_error_message(exc: Exception) -> str:
    """Category-only message for an HTTP or SSE response.

    Never returns text derived from the exception message, so no stack
    or request detail can flow to a client through this function.
    """
    if isinstance(exc, StateBusyError):
        return "another operation is already running for this collection"
    if isinstance(exc, StateError):
        return "collection state is not usable"
    if isinstance(exc, (CollectionParseError, ModParseError)):
        return "the collection manifest could not be parsed"
    if isinstance(exc, NexusAPIError):
        return "the Nexus API rejected the request"
    return "internal error"
