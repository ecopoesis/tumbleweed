"""Chat persistence: the typed SQLite store of sessions and messages (PLAN S04).

Never imports `gi` (import-linter, ARCHITECTURE section 10), and nothing of the shell: the shell
depends on `ChatHistory`, not the other way round.
"""
