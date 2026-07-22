"""Exception hierarchy for FetLifeTools."""


class FetLifeError(Exception):
    """Base class for all FetLifeTools errors."""


class AuthenticationError(FetLifeError):
    """Login failed or the session is not (or no longer) authenticated."""


class NotFoundError(FetLifeError):
    """The requested resource (member, event, group, ...) does not exist."""


class RateLimitedError(FetLifeError):
    """FetLife responded with an HTTP 429 or otherwise throttled the request."""


class ParseError(FetLifeError):
    """The page was fetched but its HTML did not match the expected structure.

    This most often means FetLife changed its markup and the relevant parser
    in ``fetlife.parsers`` needs updating.
    """
