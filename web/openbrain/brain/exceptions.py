"""Domain exceptions for the brain write services.

Views translate these to HTTP: ExperienceNotFound → 404 (indistinguishable from
a genuinely missing row — the soft-privacy rule), NotOwner → 403. Keeping them
free of any Django/HTTP import lets the service layer stay transport-agnostic and
unit-testable without a request.
"""


class ExperienceNotFound(Exception):
    """No such experience, or the viewer may not read it (private-not-mine 404)."""


class NotOwner(Exception):
    """The viewer may see the row but does not own it; writes are forbidden."""
