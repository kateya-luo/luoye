import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from .auth import CurrentUser, require_auth
from .session_manager import SessionManager


def create_sessions_router(session_manager: SessionManager, *, storage=None,
                           prefix: str = "/api/v1/sessions") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["sessions"])
    device_live_ttl = max(10, int(os.getenv("DEVICE_ACTIVE_SESSION_TTL_SECONDS", "30")))

    @router.get("/active")
    async def list_active(user: CurrentUser = Depends(require_auth)):
        active = session_manager.active_list(user.id)
        known = {item["session_id"] for item in active}
        if storage is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=device_live_ttl)).isoformat()
            rows = storage.db.query(
                "SELECT ds.server_session_id,ds.started_at_utc,ds.source_language,ds.device_id"
                " FROM device_sessions ds WHERE ds.owner_user_id=? AND ds.status='uploading'"
                " AND ds.server_session_id=(SELECT newer.server_session_id FROM device_sessions newer"
                " WHERE newer.device_id=ds.device_id AND newer.status='uploading'"
                " ORDER BY newer.started_at_utc DESC,newer.created_at DESC LIMIT 1)"
                " AND EXISTS (SELECT 1 FROM device_audio_chunks chunk"
                " WHERE chunk.server_session_id=ds.server_session_id AND chunk.created_at>=?)"
                " ORDER BY ds.started_at_utc DESC",
                (user.id, cutoff),
            )
            active.extend({
                "session_id": row["server_session_id"],
                "started_at": row["started_at_utc"],
                "segment_count": len(storage.load_segments(row["server_session_id"])),
                "observer_count": 0,
                "language": row["source_language"],
                "source": "device",
                "device_id": row["device_id"],
            } for row in rows if row["server_session_id"] not in known)
        return {"sessions": active}

    return router
