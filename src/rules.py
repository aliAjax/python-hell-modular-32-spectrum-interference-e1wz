import math
from datetime import datetime

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
# 复测强度不高于该阈值（dBm）视为干扰消除，否则为超标
CLEARANCE_STRENGTH_DBM = -80.0
# 允许提交复测的事件状态（处置中各阶段及已结案补录）
RETEST_STATUSES = {"located", "suspended", "coordinating", "resolved"}
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
RETEST_ROLES = {"analyst", "monitor", "field_operator"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel"}


def now_iso():
    return datetime.now().astimezone().isoformat()


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assess(payload):
    strength = float(payload.get("strength_dbm", -120))
    bandwidth = max(float(payload.get("bandwidth_mhz", 0.1)), 0.001)
    impact = strength + 10.0 * math.log10(bandwidth * 1000.0)
    if impact >= -37:
        level = "critical"
    elif impact >= -50:
        level = "high"
    elif impact >= -65:
        level = "medium"
    else:
        level = "low"
    score = round(max(0.0, min(100.0, 100.0 + impact)), 2)
    return {"score": score, "level": level, "impact_value": round(impact, 2)}


def retest_cleared(strength_dbm):
    """复测强度不高于消除阈值才算合格。"""
    return float(strength_dbm) <= CLEARANCE_STRENGTH_DBM


def eligible_retests(payload, retests=None):
    """授权之后、与事件同一区域的复测，才是可用于结案判断的复测。"""
    authorized_at = payload.get("suspend_authorized_at")
    if not authorized_at:
        return []
    authorized_time = parse_iso(authorized_at)
    event_region = payload.get("region")
    result = []
    for retest in retests or []:
        if retest.get("region") != event_region:
            continue
        if parse_iso(retest["measured_at"]) <= authorized_time:
            continue
        result.append(retest)
    return result


def latest_retest(retests):
    """同一区域合格复测中时间最新的一条（同时间取后提交的）。"""
    if not retests:
        return None
    return sorted(retests, key=lambda r: (parse_iso(r["measured_at"]), r["id"]))[-1]


def resolve_readiness(item, retests=None):
    """依据授权之后、同一区域的最新复测判断能否结案。"""
    payload = item["payload"]
    authorized_at = payload.get("suspend_authorized_at")
    if not authorized_at:
        return {
            "can_resolve": False,
            "reason_code": "missing_authorization",
            "reason": "尚未签发停用授权，授权之后的复测才能作为结案依据",
            "clearance_strength_dbm": CLEARANCE_STRENGTH_DBM,
            "latest_retest": None,
        }
    qualified = eligible_retests(payload, retests)
    latest = latest_retest(qualified)
    if latest is None:
        return {
            "can_resolve": False,
            "reason_code": "missing_retest",
            "reason": "缺少授权之后、同一区域的复测结果，不能结案",
            "clearance_strength_dbm": CLEARANCE_STRENGTH_DBM,
            "latest_retest": None,
        }
    if not latest.get("cleared"):
        return {
            "can_resolve": False,
            "reason_code": "interference_present",
            "reason": "最新复测强度 %.1f dBm 仍高于消除阈值 %.0f dBm，干扰尚未消除"
            % (float(latest["strength_dbm"]), CLEARANCE_STRENGTH_DBM),
            "clearance_strength_dbm": CLEARANCE_STRENGTH_DBM,
            "latest_retest": latest,
        }
    return {
        "can_resolve": True,
        "reason_code": "ready",
        "reason": "最新复测强度 %.1f dBm 已不高于消除阈值 %.0f dBm，可以结案"
        % (float(latest["strength_dbm"]), CLEARANCE_STRENGTH_DBM),
        "clearance_strength_dbm": CLEARANCE_STRENGTH_DBM,
        "latest_retest": latest,
    }


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role, retests=None):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "correct_measurement":
        _need_status(item, {"pending", "assessed", "located"})
        try:
            strength = float(payload["strength_dbm"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("field_required", "strength_dbm 不能为空")
        revision = {
            "old_strength_dbm": current.get("strength_dbm"),
            "new_strength_dbm": strength,
            "reason": _text(payload, "reason"),
            "actor": actor,
        }
        current.setdefault("measurement_revisions", []).append(revision)
        current["strength_dbm"] = strength
        current["assessment"] = assess(current)
        return status, current, {"revision": revision}

    if action == "locate":
        _need_status(item, {"assessed", "located"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        current["location"] = {"label": location, "confidence": confidence}
        return "located", current, {"location": current["location"]}

    if action == "suspend":
        _need_status(item, {"located", "suspended"})
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        authorized_at = payload.get("authorized_at")
        if authorized_at is not None:
            # 校验时间格式
            parse_iso(authorized_at)
        else:
            authorized_at = now_iso()
        current["suspend_authorization"] = authorization
        current["suspend_authorized_at"] = authorized_at
        return "suspended", current, {
            "authorization_code": authorization,
            "authorized_at": authorized_at,
        }

    if action == "coordinate":
        _need_status(item, {"suspended"})
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        readiness = resolve_readiness(item, retests)
        if not readiness["can_resolve"]:
            raise DomainError(readiness["reason_code"], readiness["reason"], 409)
        latest = readiness["latest_retest"]
        evidence = _text(payload, "evidence")
        resolution = {
            "evidence": evidence,
            "cleared": True,
            "clearance_strength_dbm": CLEARANCE_STRENGTH_DBM,
            "basis_retest_id": latest["id"],
            "basis_strength_dbm": latest["strength_dbm"],
            "basis_measured_at": latest["measured_at"],
            "basis_location": latest["location"],
            "resolved_at": now_iso(),
            "actor": actor,
        }
        current["resolution"] = resolution
        current.setdefault("resolution_history", []).append(dict(resolution))
        event_payload = dict(resolution)
        return "resolved", current, event_payload

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")


def reopen_payload(item, retest):
    """结案后补录更晚的超标复测时，事件回到处置中，原结案记录保留。"""
    current = dict(item["payload"])
    reopen = {
        "retest_id": retest["id"],
        "strength_dbm": retest["strength_dbm"],
        "measured_at": retest["measured_at"],
        "location": retest["location"],
        "previous_resolution": dict(current["resolution"]),
        "reopened_at": now_iso(),
        "actor": retest.get("created_by"),
        "reason": "结案后补录的复测仍超标，事件回到处置中",
    }
    current.setdefault("reopen_history", []).append(reopen)
    # 注意：不删除 current["resolution"]，原结案记录继续保留
    event_payload = dict(reopen)
    return "coordinating", current, event_payload
