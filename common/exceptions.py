"""
A single, consistent error envelope for every API error, instead of each view
inventing its own error shape. The frontend's apiClient relies on this shape
to show error messages consistently everywhere.

Response shape on error:
{
  "error": {
    "code": "validation_error" | "not_authenticated" | "permission_denied" | ...,
    "message": "Human-readable summary.",
    "details": {...}   # optional, e.g. per-field validation errors
  }
}
"""

import logging

from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("django")

_CODE_BY_STATUS = {
    400: "validation_error",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    429: "throttled",
    500: "server_error",
}


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if response is None:
        # An unhandled exception (bug, DB error, etc.) — log it, don't leak internals to the client.
        logger.exception("Unhandled exception in %s", context.get("view"))
        return None

    detail = response.data
    message = "Something went wrong."
    details = None

    if isinstance(detail, dict):
        if "detail" in detail and len(detail) == 1:
            message = str(detail["detail"])
        else:
            details = detail
            message = "Please check the submitted data."
    elif isinstance(detail, list) and detail:
        message = str(detail[0])

    response.data = {
        "error": {
            "code": _CODE_BY_STATUS.get(response.status_code, "error"),
            "message": message,
            **({"details": details} if details else {}),
        }
    }
    return response
