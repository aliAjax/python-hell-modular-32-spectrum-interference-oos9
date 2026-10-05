import math
import uuid
from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "delegate": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel", "delegate"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel", "delegate"}
REVIEW_STATUSES = {"located", "suspended", "coordinating"}


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


def _parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed", "located", "suspended", "coordinating"})
        current["assessment"] = assess(current)
        current["review_required"] = False
        return "assessed" if status in {"pending", "assessed"} else status, current, {"assessment": current["assessment"]}

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
        return "suspended", current, {"authorization_code": authorization}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        current["resolution"] = {"evidence": _text(payload, "evidence"), "cleared": True}
        return "resolved", current, {"evidence": current["resolution"]["evidence"]}

    if action == "delegate":
        _need_status(item, {"pending", "assessed", "located", "suspended", "coordinating"})
        delegate_to = _text(payload, "delegate_to")
        delegate_region = _text(payload, "delegate_region")
        expires_at = _text(payload, "expires_at")
        try:
            expires_dt = _parse_iso(expires_at)
        except ValueError:
            raise DomainError("invalid_timestamp", "expires_at 必须是 ISO 时间")
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if expires_dt <= datetime.now(timezone.utc):
            raise DomainError("invalid_authorization", "代管授权到期时间必须晚于当前时间", 400)
        scope = payload.get("scope")
        if scope is not None and (
            not isinstance(scope, list) or not all(isinstance(entry, str) and entry.strip() for entry in scope)
        ):
            raise DomainError("invalid_scope", "scope 必须是非空字符串列表")
        delegation = {
            "id": "dlg-" + uuid.uuid4().hex[:12],
            "actor": delegate_to,
            "region": delegate_region,
            "expires_at": expires_at,
            "scope": [entry.strip() for entry in scope] if scope else None,
            "granted_by": actor,
            "granted_at": datetime.now(timezone.utc).isoformat(),
            "revoked_at": None,
            "revoke_reason": None,
        }
        current.setdefault("delegations", []).append(delegation)
        return status, current, {"delegation": delegation}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")


def plan_source_change(existing, incoming, expected_version):
    """决定一条来源记录的提交如何落库。

    existing 为 None 表示首次回传；否则包含 strength_dbm / observed_at / version。
    返回 op: insert | noop | replace | conflict。
    """
    if existing is None:
        return {"op": "insert"}
    old_strength = existing.get("strength_dbm")
    new_strength = incoming.get("strength_dbm")
    try:
        same_strength = float(old_strength) == float(new_strength)
    except (TypeError, ValueError):
        same_strength = old_strength == new_strength
    if same_strength:
        return {"op": "noop"}
    if expected_version is None:
        return {
            "op": "conflict",
            "status": 400,
            "code": "expected_version_required",
            "message": "更新来源记录需要 expected_version",
        }
    if int(expected_version) != int(existing.get("version", 1)):
        return {
            "op": "conflict",
            "status": 409,
            "code": "version_conflict",
            "message": "来源记录已被其他操作更新，请重新读取后再提交",
        }
    if incoming.get("observed_at") > existing.get("observed_at"):
        return {"op": "replace", "existing": existing, "incoming": incoming}
    return {
        "op": "conflict",
        "status": 409,
        "code": "stale_observation",
        "message": "新回传的观测时间不晚于现有记录，不能覆盖",
    }


def recompute_baseline(current, sources, status):
    """依据全部测量来源重算事件基准。

    基准取观测时间最晚的测量（来源之间同观测时间按先到为准）。
    返回 (是否变化, 变化信息)，并原地更新 current。
    """
    candidates = []
    created_obs = current.get("baseline_observed_at") or current.get("detected_at")
    candidates.append((created_obs, current.get("strength_dbm"), -1))
    for source in sources:
        payload = source.get("payload") or {}
        candidates.append((source.get("observed_at"), payload.get("strength_dbm"), source.get("id")))

    best = None
    for observed_at, strength, source_id in candidates:
        if observed_at is None or strength is None:
            continue
        if best is None or observed_at > best[0] or (observed_at == best[0] and source_id < best[2]):
            best = (observed_at, strength, source_id)
    if best is None:
        return False, None

    old_strength = current.get("strength_dbm")
    old_observed = current.get("baseline_observed_at")
    new_observed, new_strength, new_source = best
    changed = new_strength != old_strength or new_observed != old_observed
    info = {
        "old_strength_dbm": old_strength,
        "new_strength_dbm": new_strength,
        "observed_at": new_observed,
        "source_id": new_source,
        "review_required": False,
    }
    if changed:
        current["strength_dbm"] = new_strength
        current["baseline_observed_at"] = new_observed
        current["baseline_source_id"] = new_source
        info["strength_changed"] = new_strength != old_strength
        if info["strength_changed"]:
            current["assessment"] = assess(current)
            if status in REVIEW_STATUSES:
                current["review_required"] = True
                info["review_required"] = True
    return changed, info


def find_delegation(item, actor, region, now=None):
    """查找对 actor 有效的代管授权。

    返回 (delegation, state)，state 为 active | expired | none。
    """
    now = now or datetime.now(timezone.utc)
    for delegation in item.get("payload", {}).get("delegations", []):
        if delegation.get("revoked_at"):
            continue
        if delegation.get("actor") != actor:
            continue
        if region and delegation.get("region") and delegation["region"] != region:
            continue
        try:
            expires_dt = _parse_iso(delegation["expires_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if expires_dt <= now:
            return delegation, "expired"
        return delegation, "active"
    return None, "none"
