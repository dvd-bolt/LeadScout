"""Transport-independent business failures."""


class ServiceError(RuntimeError):
    """A transport-independent, user-safe business error."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
