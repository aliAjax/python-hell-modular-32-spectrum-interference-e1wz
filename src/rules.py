import math
from datetime import datetime, timezone

from . import domain
from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
# 复测强度不高于该阈值（dBm）视为干扰已消除
CLEAR_STRENGTH_DBM = -80.0
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "retest": {"field_operator", "analyst"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "retest", "resolve", "cancel"}


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


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _parse_dt(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _retest_eligibility(retest, item_region, authorized_dt):
    notes = []
    if not item_region or retest.get("region") != item_region:
        notes.append("区域不一致")
    if authorized_dt is not None:
        try:
            if not (_parse_dt(retest["measured_at"]) > authorized_dt):
                notes.append("测量时间早于授权")
        except (KeyError, ValueError):
            notes.append("测量时间无效")
    return notes


def retests_view(payload):
    """给页面/接口使用：标注每条复测是否可作为结案依据。"""
    item_region = payload.get("region")
    authorized_at = payload.get("suspend_authorized_at")
    try:
        authorized_dt = _parse_dt(authorized_at) if authorized_at else None
    except ValueError:
        authorized_dt = None
    result = []
    for retest in payload.get("retests", []):
        notes = _retest_eligibility(retest, item_region, authorized_dt)
        view = dict(retest)
        view["eligible"] = not notes
        view["excluded_reason"] = "、".join(notes)
        view["clears"] = bool(not notes and retest["strength_dbm"] <= CLEAR_STRENGTH_DBM)
        result.append(view)
    return result


def closure_evaluation(payload):
    """结案依据：授权之后、同一区域、且最新的一条复测。

    返回判定说明，供服务层随记录返回，也供页面展示结案条件。
    """
    evaluation = {
        "clear_threshold_dbm": CLEAR_STRENGTH_DBM,
        "authorized": False,
        "eligible_retests": 0,
        "latest_retest": None,
        "cleared": False,
        "reason": "尚未签发停用授权",
    }
    authorization = payload.get("suspend_authorization")
    if not authorization:
        return evaluation
    evaluation["authorized"] = True

    item_region = payload.get("region")
    authorized_at = payload.get("suspend_authorized_at")
    try:
        authorized_dt = _parse_dt(authorized_at) if authorized_at else None
    except ValueError:
        authorized_dt = None

    eligible = [
        retest
        for retest in payload.get("retests", [])
        if not _retest_eligibility(retest, item_region, authorized_dt)
    ]

    evaluation["eligible_retests"] = len(eligible)
    if not eligible:
        evaluation["reason"] = "缺少授权之后、同一区域的复测"
        return evaluation

    latest = max(eligible, key=lambda r: (_parse_dt(r["measured_at"]), r.get("seq", 0)))
    evaluation["latest_retest"] = {
        "seq": latest.get("seq"),
        "strength_dbm": latest["strength_dbm"],
        "measured_at": latest["measured_at"],
        "location": latest.get("location"),
        "region": latest.get("region"),
    }
    if latest["strength_dbm"] <= CLEAR_STRENGTH_DBM:
        evaluation["cleared"] = True
        evaluation["reason"] = "最新复测强度 %.2f dBm 已不超过 %.0f dBm，干扰已消除" % (
            latest["strength_dbm"],
            CLEAR_STRENGTH_DBM,
        )
    else:
        evaluation["reason"] = "最新复测强度 %.2f dBm 仍超过 %.0f dBm，干扰尚未消除" % (
            latest["strength_dbm"],
            CLEAR_STRENGTH_DBM,
        )
    return evaluation


def apply_action(item, action, payload, actor, role, region=None, at_iso=None):
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
        current["suspend_authorization"] = authorization
        # 授权签发时间是复测"授权之后"判定的基准
        current.setdefault("suspend_authorized_at", at_iso)
        return "suspended", current, {
            "authorization_code": authorization,
            "authorized_at": current["suspend_authorized_at"],
        }

    if action == "coordinate":
        _need_status(item, {"suspended"})
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "retest":
        _need_status(item, {"suspended", "coordinating", "resolved"})
        retest = domain.normalize_retest(payload, region)
        retest["seq"] = len(current.get("retests", [])) + 1
        retest["actor"] = actor
        current.setdefault("retests", []).append(retest)

        new_status = status
        event_payload = {
            "retest_seq": retest["seq"],
            "strength_dbm": retest["strength_dbm"],
            "measured_at": retest["measured_at"],
            "location": retest["location"],
            "region": retest["region"],
            "reopened": False,
        }
        evaluation = closure_evaluation(current)
        latest = evaluation["latest_retest"]
        # 已结案后补录：只有更晚的同区超标复测成为最新依据时才翻案；旧结果只进历史
        if (
            status == "resolved"
            and latest
            and latest.get("seq") == retest["seq"]
            and retest["strength_dbm"] > CLEAR_STRENGTH_DBM
        ):
            previous_resolution = current.pop("resolution", None)
            if previous_resolution:
                previous_resolution["superseded_at"] = at_iso
                previous_resolution["superseded_by_retest_seq"] = retest["seq"]
                current.setdefault("resolution_history", []).append(previous_resolution)
            new_status = "coordinating"
            event_payload["reopened"] = True
            event_payload["reason"] = "结案后补录的更晚复测强度仍超标，事件回到处置中"
        return new_status, current, event_payload

    if action == "resolve":
        _need_status(item, {"coordinating"})
        evaluation = closure_evaluation(current)
        if not evaluation["authorized"]:
            raise DomainError("suspend_authorization_required", "尚未签发停用授权，不能结案", 409)
        latest = evaluation["latest_retest"]
        if not latest:
            raise DomainError("retest_required", "缺少授权之后、同一区域的复测证据，不能结案", 409)
        if latest["strength_dbm"] > CLEAR_STRENGTH_DBM:
            raise DomainError("interference_present", "最新复测强度仍超标，干扰尚未消除，不能结案", 409)
        evidence = _text(payload, "evidence")
        current["resolution"] = {
            "evidence": evidence,
            "cleared": True,
            "retest": latest,
            "resolved_by": actor,
            "resolved_at": at_iso,
        }
        return "resolved", current, {
            "evidence": evidence,
            "retest": latest,
        }

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
