import os

# Two review workers: enough to exercise the process pool, cheap to spawn.
os.environ.setdefault("REVIEW_WORKERS", "2")
