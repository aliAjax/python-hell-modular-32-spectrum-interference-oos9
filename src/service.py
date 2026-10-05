from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def _has_region_access(self, actor, role, region, item_id=None):
        if not rules.ENFORCE_REGION or not region or role == "regulator":
            return True
        delegation = self.repository.find_active_delegation(actor, region, item_id=item_id)
        return delegation is not None

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        source_region = normalized.get("region") or item["payload"].get("region")
        if rules.ENFORCE_REGION and region and role != "regulator" and source_region and source_region != region:
            if not self._has_region_access(actor, role, source_region, item_id=item_id):
                raise DomainError("region_mismatch", "来源记录不属于当前管辖区域，且无有效代管授权", 403)
        return self.repository.add_source(item_id, normalized, actor, role)

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and region and role != "regulator":
            item_region = item["payload"].get("region")
            if item_region and item_region != region and not self._has_region_access(
                actor, role, item_region, item_id=item_id
            ):
                raise DomainError("region_mismatch", "不能处理其他区域的记录，且无有效代管授权", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def grant_delegation(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in {"coordinator", "regulator"}:
            raise DomainError("forbidden", "只有协调人可以授予代管授权", 403)
        normalized = domain.normalize_delegation(payload)
        if role != "regulator" and region and normalized["region"] != region:
            raise DomainError("region_mismatch", "不能为其他区域授予代管授权", 403)
        if normalized["item_id"] is not None:
            target = self.repository.get_item(normalized["item_id"])
            if role != "regulator" and target["payload"].get("region") != normalized["region"]:
                raise DomainError("region_mismatch", "代管区域必须与事件区域一致", 403)
        return self.repository.create_delegation(normalized, actor, role)

    def list_delegations(self, actor, role, region=None, grantee=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        filter_region = None if role == "regulator" else region
        filter_grantee = actor if role not in {"coordinator", "regulator"} else grantee
        return self.repository.list_delegations(grantee=filter_grantee, region=filter_region)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        review = item["payload"].get("review_required")
        item["review_required"] = review
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
