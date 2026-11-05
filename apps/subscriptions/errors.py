class RequestRejected(Exception):
    """A request that can't be recorded. `code` is stable for the API; the message is safe to show."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
