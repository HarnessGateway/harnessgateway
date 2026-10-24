"""Layer 3, the domain: what the platform keeps and the rules it keeps it by, one object per module.
Each module holds a frozen dataclass and the class that keeps it in records and blobs; nothing here
runs anything or speaks HTTP. A platform-level record (a login, a worker, a token) belongs to the
empty tenant."""

PLATFORM = ""                                        # the tenant of platform-level records
