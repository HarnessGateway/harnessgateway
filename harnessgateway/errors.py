"""What the platform refuses, and how a refusal is told to the caller (spec §13.7)."""


class Refused(Exception):
    """A request the platform will not carry out. The message names what was wrong and what is allowed."""

    status = 400
    type = "invalid_request_error"

    def __init__(self, message: str, *, param: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.code = code


class Invalid(Refused):
    """The request, a definition or a name is not valid."""


class Unauthorized(Refused):
    status = 401
    type = "invalid_api_key"


class Forbidden(Refused):
    status = 403
    type = "insufficient_permissions"


class NotFound(Refused):
    status = 404
    type = "not_found"


class Conflict(Refused):
    status = 409
    type = "conflict"


class TooLarge(Refused):
    status = 413
    type = "request_too_large"
