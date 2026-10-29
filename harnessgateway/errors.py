"""Errors a caller sees, in OpenAI's shape: {"error": {"type", "code", "message", "param"}}."""
from __future__ import annotations


class ApiError(Exception):
    status = 500
    type = "server_error"

    def __init__(self, message: str, *, code: str | None = None, param: str | None = None):
        super().__init__(message)
        self.message, self.code, self.param = message, code, param

    def body(self) -> dict[str, object]:
        return {"error": {"type": self.type, "code": self.code, "message": self.message, "param": self.param}}


class Invalid(ApiError):
    status, type = 400, "invalid_request_error"


class Unauthenticated(ApiError):
    status, type = 401, "authentication_error"


class Forbidden(ApiError):
    status, type = 403, "permission_error"


class NotFound(ApiError):
    status, type = 404, "not_found_error"


class Conflict(ApiError):
    status, type = 409, "conflict_error"


class TooLarge(ApiError):
    status, type = 413, "invalid_request_error"


class Refused(ApiError):
    """An extension's refusal of a turn (a quota, a plan): 429 by default, or the status it names."""
    status, type = 429, "rate_limit_error"
