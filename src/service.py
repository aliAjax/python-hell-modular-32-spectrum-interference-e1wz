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

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def _enforce_retest_region(self, item, region, role):
        if rules.ENFORCE_REGION and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能在其他区域提交复测", 403)

    def add_retest(self, item_id, payload, actor, role, region=None):
        """现场人员提交复测（强度、时间、位置）。
        已结案事件若补录一条授权之后、同区域且更晚的超标复测，自动回到处置中。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.RETEST_ROLES:
            raise DomainError("forbidden", "当前角色不能提交复测", 403)
        item = self.repository.get_item(item_id)
        if item["status"] not in rules.RETEST_STATUSES:
            raise DomainError("invalid_state", "当前状态 %s 不允许提交复测" % item["status"])
        self._enforce_retest_region(item, region, role)
        normalized = domain.normalize_retest(payload)
        normalized["region"] = item["payload"].get("region")
        cleared = rules.retest_cleared(normalized["strength_dbm"])

        def reopen_builder(new_retest, locked_item):
            new_retest = dict(new_retest, cleared=cleared)
            if locked_item["status"] != "resolved":
                return None
            existing = [
                r for r in self.repository.list_retests(item_id) if int(r["id"]) != int(new_retest["id"])
            ]
            qualified = rules.eligible_retests(locked_item["payload"], existing)
            previous_latest = rules.latest_retest(qualified)
            # 只有新复测成为“授权之后、同区域”的最新复测且超标时，才推翻结案；
            # 晚到的旧结果只进入复测历史，不改变当前结论。
            if cleared:
                return None
            if previous_latest and rules.parse_iso(previous_latest["measured_at"]) > rules.parse_iso(
                new_retest["measured_at"]
            ):
                return None
            return rules.reopen_payload(locked_item, new_retest)

        retest = self.repository.add_retest(item_id, normalized, actor, role, reopen_builder)
        retest["cleared"] = cleared
        return retest

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        retests = self.repository.list_retests(item_id) if action == "resolve" else None
        if retests is not None:
            for retest in retests:
                retest["cleared"] = rules.retest_cleared(retest["strength_dbm"])
        new_status, new_payload, event_payload = rules.apply_action(
            item, action, payload, actor, role, retests
        )
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["retests"] = self.repository.list_retests(item_id)
        for retest in item["retests"]:
            retest["cleared"] = rules.retest_cleared(retest["strength_dbm"])
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["resolve_readiness"] = rules.resolve_readiness(item, item["retests"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
