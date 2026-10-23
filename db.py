from supabase import create_client
import config

sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)


def _norm(ch: str) -> str:
    ch = (ch or "").strip()
    for p in ("https://t.me/", "http://t.me/", "t.me/", "@"):
        if ch.startswith(p):
            ch = ch[len(p):]
    return ch.strip("/").lower()


def _clean_kw(w: str) -> str:
    return " ".join((w or "").split())  # trim + collapse inner spaces


def get_channels() -> list[str]:
    return [r["username"] for r in sb.table("source_channels").select("username").execute().data]


def add_channel(ch): sb.table("source_channels").upsert({"username": _norm(ch)}, on_conflict="username").execute()


def remove_channel(ch) -> bool:
    return bool(sb.table("source_channels").delete().eq("username", _norm(ch)).execute().data)


def get_keywords() -> list[str]:
    return [r["word"] for r in sb.table("keywords").select("word").order("id").execute().data]


def _find_keyword(w: str):
    """Exact, case-insensitive lookup (done in Python so % and _ are not wildcards)."""
    lw = w.lower()
    for r in sb.table("keywords").select("id,word").execute().data:
        if r["word"].lower() == lw:
            return r
    return None


def add_keyword(w) -> bool:
    """Returns False if the keyword already exists (any letter case)."""
    w = _clean_kw(w)
    if not w or _find_keyword(w):
        return False
    sb.table("keywords").insert({"word": w}).execute()
    return True


def remove_keyword(w) -> bool:
    r = _find_keyword(_clean_kw(w))
    if not r:
        return False
    sb.table("keywords").delete().eq("id", r["id"]).execute()
    return True


def get_setting(key, default=None):
    d = sb.table("settings").select("value").eq("key", key).execute().data
    return d[0]["value"] if d else default


def set_setting(key, value):
    sb.table("settings").upsert({"key": key, "value": str(value)}, on_conflict="key").execute()


def claim_post(chat, msg_id) -> bool:
    """Atomically reserve a post BEFORE processing. The unique (source_chat, message_id)
    constraint guarantees only one worker/event can ever win, so nothing posts twice."""
    try:
        sb.table("processed_posts").insert(
            {"source_chat": chat, "message_id": msg_id, "status": "processing"}
        ).execute()
        return True
    except Exception as e:
        if "duplicate" in str(e).lower() or "23505" in str(e):
            return False
        raise


def already_processed(chat, msg_id) -> bool:
    return bool(sb.table("processed_posts").select("id").eq("source_chat", chat).eq("message_id", msg_id).execute().data)


def log_post(chat, msg_id, status, output=None, error=None):
    sb.table("processed_posts").upsert(
        {"source_chat": chat, "message_id": msg_id, "status": status, "output": output, "error": error},
        on_conflict="source_chat,message_id",
    ).execute()
