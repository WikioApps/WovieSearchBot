"""Optional atomic per-user admission limits for durable delivery batches."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib

from pymongo.errors import DuplicateKeyError

OUTSTANDING = ("queued", "running", "paused", "failed")
MAX_USER_BATCHES = 3


class BatchQueueFull(Exception):
    """A new request was not admitted; previously accepted work remains intact."""


async def enqueue_batch(store, payload: dict, key: str) -> str:
    """Reserve capacity and save work in one atomic MongoDB document insertion.

    Store.connect creates the unique partial user_batch_slots index. Its active
    status predicate frees a slot only when that batch finishes or is cancelled.
    """
    actor = payload.get("actor_id")
    if not isinstance(actor, int) or isinstance(actor, bool) or actor <= 0 or not key:
        raise ValueError(
            "A delivery batch requires an identified user and request key."
        )
    job_id = hashlib.sha256(f"batch:{key}".encode()).hexdigest()[:32]
    identity = {"_id": job_id, "kind": "batch", "payload.actor_id": actor}
    if await store.db.jobs.find_one(identity, {"_id": 1}):
        return job_id
    config = await store.setting(0)
    if not config.get("batch_admission_enabled", False):
        return await store.enqueue("batch", payload, key=key)

    # Work accepted before opt-in still occupies capacity. Never cancel, truncate
    # or drop those jobs to make the configured limit appear satisfied.
    legacy = await store.db.jobs.count_documents(
        {
            "kind": "batch",
            "payload.actor_id": actor,
            "status": {"$in": list(OUTSTANDING)},
            "admission_slot": {"$exists": False},
        },
        limit=1,
        maxTimeMS=5000,
    )
    if legacy:
        raise BatchQueueFull(
            "An earlier batch is still outstanding. Let it finish before starting "
            "another; if it is paused or failed, ask an administrator to check /jobs. "
            "Your accepted work is preserved."
        )
    now = datetime.now(UTC)
    for slot in range(MAX_USER_BATCHES):
        row = {
            "_id": job_id,
            "kind": "batch",
            "payload": payload,
            "status": "queued",
            "run_at": now,
            "lease_until": now,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
            "admission_slot": slot,
        }
        try:
            await store.db.jobs.insert_one(row)
            return job_id
        except DuplicateKeyError:
            # A concurrent replay may already have inserted this exact request.
            if await store.db.jobs.find_one(identity, {"_id": 1}):
                return job_id
    raise BatchQueueFull(
        "You already have three outstanding batches. Please let them finish before "
        "sending another. If delivery is paused or failed, ask an administrator to "
        "check /jobs. Your previously accepted batches are preserved."
    )
