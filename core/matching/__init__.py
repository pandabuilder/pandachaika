"""Archive and wanted gallery matching.

Matching finds the galleries an archive file (or a title) belongs to. It runs in one of two modes:

* online: the provider matchers (each provider's ``matchers.py``) search the provider's site, and every request goes
  through the shared database rate limiter (``request_by_provider``), the same one parsers and downloaders use.
* offline: the local matchers in :mod:`core.matching.local` compare against the galleries already in the database.

Both modes produce :class:`~core.matching.types.MatchCandidate` objects. :class:`~core.matching.engine.MatchingEngine`
runs either mode, and can store the candidates as possible matches or link the archive to the best one (auto-link).
"""

from core.matching.types import MatchCandidate, MatchingMode, MatchTarget, SearchResult

__all__ = ["MatchCandidate", "MatchingMode", "MatchTarget", "SearchResult"]
