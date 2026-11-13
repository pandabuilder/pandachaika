class ProviderBannedError(Exception):
    """A provider temporarily banned our IP, and the ban is too long to wait for inside a running task.

    Tasks that get this are put back in the queue to start after ``retry_after`` seconds.
    """

    def __init__(self, provider: str, retry_after: float, url: str = "") -> None:
        self.provider = provider
        self.retry_after = max(0.0, float(retry_after))
        self.url = url
        super().__init__(f"Provider {provider!r} is temporarily banning requests, retry in {self.retry_after:.0f}s")
