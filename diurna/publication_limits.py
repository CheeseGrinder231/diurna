"""Known publication resource ceilings, independent of classifier initialization."""


class PublicationWaiting(ValueError):
    """Resource ceiling reached; no corruption or durable intervention latch."""
