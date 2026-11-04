"""Domain errors, mapped to HTTP responses in ``app.main``."""


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(DomainError):
    status_code, code = 404, "not_found"


class ValidationFailed(DomainError):
    status_code, code = 422, "validation_failed"


class Conflict(DomainError):
    status_code, code = 409, "conflict"


class InvalidState(Conflict):
    code = "invalid_state"


class InsufficientStock(Conflict):
    code = "insufficient_stock"


class CapacityExceeded(Conflict):
    code = "capacity_exceeded"


class OverReceipt(Conflict):
    code = "over_receipt"


class ConcurrencyConflict(Conflict):
    code = "concurrency_conflict"
