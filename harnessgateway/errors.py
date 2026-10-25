"""Refusals, by kind. Every layer raises these for what a caller asked wrong; the HTTP layer maps each kind to one
status code and the OpenAI error envelope. Anything else that escapes is a bug and becomes a 500."""
from __future__ import annotations


class Refusal(Exception):
    status = 400
    type = "invalid_request_error"

    def __init__(self, message: str, *, code: str | None = None, param: str | None = None):
        super().__init__(message)
        self.message, self.code, self.param = message, code, param


class Invalid(Refusal):
    status = 400


class Unauthenticated(Refusal):
    status = 401
    type = "authentication_error"


class Forbidden(Refusal):
    status = 403
    type = "permission_error"


class NotFound(Refusal):
    status = 404
    type = "not_found_error"


class Conflict(Refusal):
    status = 409
    type = "conflict_error"


class TooLarge(Refusal):
    status = 413


class QuotaExceeded(Refusal):
    status = 429
    type = "rate_limit_error"


class Unavailable(Refusal):
    status = 503
    type = "service_unavailable"
