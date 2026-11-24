"""Refusals: what the platform says when it will not do what was asked.

Each kind carries its HTTP status and the `type` it has in the OpenAI error envelope, so the API has one way to
turn any of them into a response. A refusal's message names what was wrong and what is allowed.
"""


class Refused(Exception):
    status = 400
    type = "invalid_request_error"

    def __init__(self, message: str, *, param: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param                     # the field of the request that was wrong, when there is one
        self.code = code


class Invalid(Refused):
    """The request is not one the platform accepts."""


class Unauthorized(Refused):
    status = 401
    type = "authentication_error"


class Forbidden(Refused):
    status = 403
    type = "permission_error"


class NotFound(Refused):
    status = 404
    type = "not_found_error"


class Conflict(Refused):
    status = 409
    type = "conflict_error"


class TooLarge(Refused):
    status = 413
    type = "request_too_large"
