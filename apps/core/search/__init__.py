"""
In-database full-text search for catalog entries (IP-020, issue #101).

- `ddl`: PostgreSQL text search configurations and helper functions the index depends on.
- `index`: maintains `entries.search_vector` / `entries.search_config`.
- `query`: turns the reader's words into a tsquery, filters, ranks, highlights and falls back.
"""
