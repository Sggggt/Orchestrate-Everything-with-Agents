"""Offline addition of user task handling, preserving execution facts."""

PRE_HANDLING_SCHEMA = '6e2d834852d8d215408789d4d1966337b806bc96bd9e00dd1e17fe80644ecefa'


def upgrade_handling(db):
    db.execute("ALTER TABLE runs ADD COLUMN handled_at INTEGER")
    db.execute("CREATE INDEX usage_attention ON usage(run_id) WHERE state='needs_review'")
