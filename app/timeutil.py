"""时间工具：交换时间一律使用带偏移量的 ISO 8601 字符串。"""

from datetime import datetime, timezone


def parse_occurred_at(value: object) -> datetime:
    """解析业务事实的实际发生时间；拒绝没有时区偏移量的时间。"""
    if not isinstance(value, str) or not value:
        raise ValueError("occurred_at 必须是带偏移量的 ISO 8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("occurred_at 必须是带偏移量的 ISO 8601 字符串") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("occurred_at 必须显式携带 UTC 偏移量（例如 +08:00）")
    return dt


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
