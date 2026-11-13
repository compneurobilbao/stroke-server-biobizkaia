class ProcessorCanceled(Exception):
    """Raised when the running external processor is interrupted."""

    def __init__(self, message: str = "Processing canceled") -> None:
        super().__init__(message)
        self.message = message
