"""Layer 0: refusals, by kind. Every layer raises these for what a caller asked wrong; a front end decides
what each means on its wire (over HTTP: 400, 404 and 409). Anything else that goes wrong is a bug."""


class Invalid(ValueError):
    """The request can't be carried out as given."""


class NotFound(LookupError):
    """It names something that doesn't exist."""


class Conflict(Exception):
    """It collides with something already in progress."""
