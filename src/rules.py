import math
from datetime import datetime, timezone

from .domain import ConflictError, DomainError, parse_dt

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
    "acknowledge_review": {"analyst", "monitor", "field_operator", "coordinator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel"}

# 基准测量变更后，已经进入定位或处置阶段的事件需要人工复核
REVIEW_STATUSES = {"located", "suspended", "coordinating"}

REVIEW_MESSAGE = "基准测量已按更晚观测更新，等级已重算，请复核定位与处置结论"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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


def decide_source(existing, observed_at, strength):
    """同一 (source_type, external_id) 重复回传的取舍。

    返回：
      "insert"     首次回传，直接落库
      "superseded" 观测时间更晚且强度不同，替换既有记录
      "ignored"    观测时间更晚但强度一致，去重，不落新内容
    观测更早或同一时刻后到的，拒绝（ConflictError）。
    """
    if existing is None:
        return "insert"
    old_obs = parse_dt(existing["observed_at"])
    new_obs = parse_dt(observed_at)
    if new_obs < old_obs:
        raise ConflictError(
            "stale_measurement",
            "该来源已有观测时间更晚的测量记录，较早观测的补录不予采纳",
        )
    if new_obs == old_obs:
        raise ConflictError(
            "simultaneous_measurement",
            "该来源同一观测时刻的记录已先到，后到的补录不能覆盖先到的",
        )
    if abs(float(existing["payload"]["strength_dbm"]) - float(strength)) < 1e-9:
        return "ignored"
    return "superseded"


def _measurement_candidates(payload, sources, item_created_at):
    """汇总事件的全部测量依据：创建测量、各来源当前测量、人工更正。"""
    candidates = [
        {
            "ref": "creation",
            "strength_dbm": payload.get("initial_strength_dbm", payload.get("strength_dbm")),
            "observed_at": payload["detected_at"],
            "recorded_at": item_created_at,
        }
    ]
    for source in sources:
        candidates.append(
            {
                "ref": "source:%s" % source["id"],
                "strength_dbm": source["payload"]["strength_dbm"],
                "observed_at": source["observed_at"],
                "recorded_at": source["created_at"],
            }
        )
    for index, revision in enumerate(payload.get("measurement_revisions", [])):
        if revision.get("recorded_at"):
            candidates.append(
                {
                    "ref": "revision:%d" % index,
                    "strength_dbm": revision["new_strength_dbm"],
                    "observed_at": revision["observed_at"],
                    "recorded_at": revision["recorded_at"],
                }
            )
    return candidates


def select_baseline(candidates):
    """观测更晚的作准；观测同时刻时先入库（到达更早）的作准。"""
    return min(
        candidates,
        key=lambda c: (
            parse_dt(c["observed_at"]).timestamp() * -1.0,
            parse_dt(c["recorded_at"]).timestamp(),
        ),
    )


def apply_baseline(current, status, winner, recorded_at, actor, role, reason):
    """按选出的测量候选更新事件基准。返回变更说明；强度未变返回 None。

    只有曾正式评估（payload.assessment 存在）时，基准变更才使旧评估失效并按新
    强度重算；处于定位/处置阶段的事件会置复核提示。
    """
    new_strength = float(winner["strength_dbm"])
    current["baseline_basis"] = {"ref": winner["ref"], "observed_at": winner["observed_at"]}
    old_strength = current.get("strength_dbm")
    if old_strength is not None and abs(float(old_strength) - new_strength) < 1e-9:
        return None

    was_assessed = "assessment" in current and current.get("assessment") is not None
    old_assessment = current.get("assessment") if was_assessed else None
    if old_assessment is not None:
        current.setdefault("assessment_history", []).append(
            {
                "assessment": old_assessment,
                "strength_dbm": float(old_strength),
                "invalidated_at": recorded_at,
                "reason": reason,
            }
        )

    revision = {
        "old_strength_dbm": None if old_strength is None else float(old_strength),
        "new_strength_dbm": new_strength,
        "reason": reason,
        "actor": actor,
        "recorded_at": recorded_at,
        "observed_at": winner["observed_at"],
        "basis_ref": winner["ref"],
    }
    current.setdefault("measurement_revisions", []).append(revision)
    current["strength_dbm"] = new_strength
    # 按新强度试算等级，供复核提示展示；未正式评估过则不固化 assessment
    trial_assessment = assess(current)
    if was_assessed:
        current["assessment"] = trial_assessment

    review_required = was_assessed and status in REVIEW_STATUSES
    if review_required:
        current["review_required"] = {
            "since": recorded_at,
            "actor": actor,
            "role": role,
            "old_strength_dbm": revision["old_strength_dbm"],
            "new_strength_dbm": new_strength,
            "old_level": old_assessment["level"],
            "new_level": trial_assessment["level"],
            "message": REVIEW_MESSAGE,
        }
    return {
        "old_strength_dbm": revision["old_strength_dbm"],
        "new_strength_dbm": new_strength,
        "old_level": None if old_assessment is None else old_assessment["level"],
        "new_level": trial_assessment["level"],
        "basis_ref": winner["ref"],
        "review_required": review_required,
    }


def recompute_baseline(payload, status, sources, item_created_at, recorded_at, actor, role, reason):
    candidates = _measurement_candidates(payload, sources, item_created_at)
    winner = select_baseline(candidates)
    return apply_baseline(payload, status, winner, recorded_at, actor, role, reason)


def apply_action(item, action, payload, actor, role):
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
        reason = _text(payload, "reason")
        recorded_at = now_iso()
        # 人工更正直接确立为当前基准（更正时刻入库），不参与自动重选
        winner = {
            "ref": "manual_correction",
            "strength_dbm": strength,
            "observed_at": recorded_at,
        }
        change = apply_baseline(current, status, winner, recorded_at, actor, role, reason)
        if change is None:
            # 强度与基准一致：仍然保留更正动作留痕
            revision = {
                "old_strength_dbm": float(current["strength_dbm"]),
                "new_strength_dbm": strength,
                "reason": reason,
                "actor": actor,
                "recorded_at": recorded_at,
                "observed_at": recorded_at,
                "basis_ref": "manual_correction",
                "unchanged": True,
            }
            current.setdefault("measurement_revisions", []).append(revision)
            change = {"unchanged": True, "new_strength_dbm": strength}
        return status, current, {"revision": current["measurement_revisions"][-1], "change": change}

    if action == "acknowledge_review":
        _need_status(item, REVIEW_STATUSES)
        if not current.get("review_required"):
            raise DomainError("no_review_pending", "当前没有待复核的基准变更")
        note = payload.get("note")
        if note is not None:
            note = str(note).strip() or None
        review = current.pop("review_required")
        acknowledgement = {
            "review": review,
            "actor": actor,
            "note": note,
            "acknowledged_at": now_iso(),
        }
        current.setdefault("review_acknowledgements", []).append(acknowledgement)
        return status, current, {"acknowledged": acknowledgement}

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

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
