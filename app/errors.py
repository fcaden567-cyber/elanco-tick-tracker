"""Consistent JSON error envelope for every failure the API can return.

Every error looks like:
    {"error": {"code": "invalid_parameter", "message": "...", "details": ..., "request_id": "..."}}
so clients only ever need one parser, and the request id ties a report back to our logs.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _body(request: Request, code: str, message: str, details: Any = None) -> dict:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    error["request_id"] = getattr(request.state, "request_id", None)
    return {"error": error}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return JSONResponse(_body(request, exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in err["loc"] if p != "query"), "problem": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            _body(request, "invalid_parameter", "One or more parameters are invalid.", details), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(_body(request, code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Never leak stack traces to clients; the request id lets us find it in logs.
        log.exception("Unhandled error (request_id=%s)", getattr(request.state, "request_id", None))
        return JSONResponse(
            _body(request, "internal_error", "Something went wrong on our side. Please try again."), status_code=500
        )
