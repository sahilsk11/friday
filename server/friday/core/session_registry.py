"""SessionRegistry — in-memory dict + sqlite persistence.

Sessions survive server restart; rehydrate from sqlite and re-subscribe to
opencode SSE for any session that was running.
"""
