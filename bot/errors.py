class Cancelled(Exception):
    """Raised inside a job when the user cancels it."""


class MediaError(RuntimeError):
    """ffprobe / ffmpeg failed."""


class FormatError(Exception):
    """A saved-format operation was refused; ``code`` is ``limit`` (12 formats already) or ``taken`` (name in use)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
