"""FetLifeTools — command-line tools for querying the FetLife website."""

from .client import FetLifeClient
from .exceptions import (
    AuthenticationError,
    FetLifeError,
    NotFoundError,
    ParseError,
    RateLimitedError,
)

__version__ = "0.1.0"

__all__ = [
    "FetLifeClient",
    "FetLifeError",
    "AuthenticationError",
    "NotFoundError",
    "ParseError",
    "RateLimitedError",
    "__version__",
]
