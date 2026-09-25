"""定义云赏月互动内容治理的类别、可见范围与投稿载荷校验。"""

from __future__ import annotations

from typing import Any

from festival_foundation.errors import ValidationError

KINDS = frozenset({"poetry_chain", "hometown_intro", "blessing"})
SCOPE_RANK = {"internal": 1, "event": 2, "public": 3}
SCOPES = frozenset(SCOPE_RANK)
MEDIA_TYPES = frozenset({"image", "video", "audio"})
TERMINAL_STATUSES = frozenset({"rejected", "withdrawn", "compliance_removed"})
BLOCKED_STATUSES = frozenset({"withdrawn", "compliance_removed"})

TEXT_LIMIT = 2000
REASON_LIMIT = 500
TITLE_LIMIT = 120


def validate_kind(value: Any) -> str:
    """校验投稿类别。"""
    kind = str(value).strip()
    if kind not in KINDS:
        raise ValidationError("kind 必须是 poetry_chain、hometown_intro 或 blessing")
    return kind


def validate_scope(value: Any) -> str:
    """校验可见范围。"""
    scope = str(value).strip()
    if scope not in SCOPES:
        raise ValidationError("可见范围必须是 public、event 或 internal")
    return scope


def scope_covers(grant: str, audience: str) -> bool:
    """判断投稿授权范围是否覆盖专题受众范围。"""
    return SCOPE_RANK[grant] >= SCOPE_RANK[audience]


def validate_text(value: Any, field: str = "text_body", limit: int = TEXT_LIMIT) -> str:
    """校验一段必填文本。"""
    text = str(value).strip()
    if not text or len(text) > limit:
        raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
    return text


def validate_media(value: Any) -> list[dict[str, Any]]:
    """校验媒体摘要列表，服务只登记摘要，不接触图片或视频本体。"""
    if not isinstance(value, list):
        raise ValidationError("media_summaries 必须是数组")
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValidationError(f"媒体摘要第 {index + 1} 项必须是对象")
        media_id = str(item.get("media_id", "")).strip()
        if not media_id:
            raise ValidationError(f"媒体摘要第 {index + 1} 项缺少 media_id")
        media_type = str(item.get("media_type", "")).strip()
        if media_type not in MEDIA_TYPES:
            raise ValidationError(f"媒体摘要第 {index + 1} 项 media_type 无效")
        checksum = str(item.get("checksum", "")).strip()
        if not checksum:
            raise ValidationError(f"媒体摘要第 {index + 1} 项缺少 checksum 摘要")
        entry: dict[str, Any] = {"media_id": media_id, "media_type": media_type, "checksum": checksum}
        for optional in ("mime", "width", "height", "duration_seconds", "size_bytes"):
            if optional in item and item[optional] is not None:
                entry[optional] = item[optional]
        normalized.append(entry)
    return normalized


def validate_declaration(value: Any) -> dict[str, Any]:
    """校验作者声明，必须包含原创确认与授权说明。"""
    if not isinstance(value, dict):
        raise ValidationError("declaration 必须是对象")
    original = value.get("original")
    if not isinstance(original, bool):
        raise ValidationError("declaration.original 必须是布尔值")
    grant = str(value.get("grant", "")).strip()
    if not grant or len(grant) > 200:
        raise ValidationError("declaration.grant 不能为空且不能超过 200 个字符")
    declaration: dict[str, Any] = {"original": original, "grant": grant}
    notes = value.get("notes")
    if notes is not None:
        declaration["notes"] = validate_text(notes, "declaration.notes", 500)
    return declaration


def validate_citations(value: Any) -> list[dict[str, Any]]:
    """校验来源引用列表。"""
    if not isinstance(value, list):
        raise ValidationError("citations 必须是数组")
    normalized = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValidationError(f"来源引用第 {index + 1} 项必须是对象")
        reference = str(item.get("reference", "")).strip()
        if not reference or len(reference) > 200:
            raise ValidationError(f"来源引用第 {index + 1} 项 reference 不能为空且不能超过 200 个字符")
        entry: dict[str, Any] = {"reference": reference}
        source_type = item.get("source_type")
        if source_type is not None:
            entry["source_type"] = str(source_type).strip()
        normalized.append(entry)
    return normalized
