"""One definition of user attention, separate from execution and accounting facts."""

# Callers use the runs alias r. The indexed usage lookup also finds incomplete
# accounting on finished work, which execution-state filters alone would omit.
# CASE keeps keyset pagination in row order instead of combining state indexes
# and sorting the entire history before applying LIMIT.
ATTENTION_REASON = """(CASE WHEN r.state IN ('waiting','failed','needs_review') THEN 1
    WHEN r.state IN ('completed','cancelled') THEN EXISTS
     (SELECT 1 FROM usage u WHERE u.run_id=r.id AND u.state='needs_review') ELSE 0 END)"""
NEEDS_ATTENTION = "(r.handled_at IS NULL AND " + ATTENTION_REASON + ")"
