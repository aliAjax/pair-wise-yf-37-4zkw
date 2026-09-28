import re
from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


FOLLOWUP_DAYS = 14

# 登记随访时可选的常见症状编码（其他编码也会被原样保留）
SYMPTOM_LABELS = {
    "fever": "发热",
    "cough": "咳嗽",
    "sore_throat": "咽痛",
    "fatigue": "乏力",
    "diarrhea": "腹泻",
    "dyspnea": "呼吸困难",
    "other": "其他",
}
_EMPTY_SYMPTOM_TOKENS = {"", "none", "no", "asymptomatic", "无", "无症状", "无异常", "正常"}
_SPLIT_RE = re.compile(r"[,，;；、\s]+")


def parse_day(value, field="date"):
    if value is None or value == "":
        raise ValidationError("missing required field: " + field)
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        raise ValidationError("%s 不是有效日期(YYYY-MM-DD): %r" % (field, value))


def to_iso(day):
    return day.isoformat()


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_symptoms(value):
    """接受列表或分隔字符串，返回去重后的症状编码；空表示无症状。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [item for item in _SPLIT_RE.split(value) if item]
    else:
        items = list(value)
    result = []
    for item in items:
        token = str(item).strip().lower()
        if token in _EMPTY_SYMPTOM_TOKENS:
            continue
        if token not in result:
            result.append(token)
    return result


def last_exposure_day(data):
    """最后接触日：优先 exposure_end，缺省回退到 exposure_start。"""
    value = data.get("exposure_end") or data.get("exposure_start")
    return parse_day(value, "exposure date")


def observation_window(exposure_end_day):
    """从最后接触日次日起算，满 14 天。返回 (起算日, 到期日)。"""
    return (
        exposure_end_day + timedelta(days=1),
        exposure_end_day + timedelta(days=FOLLOWUP_DAYS),
    )


def _date_ordinal(value):
    return parse_day(value).toordinal()


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _current_window(data):
    """当前允许登记随访的日期范围。

    下限为观察期起算日；上限为最近一次异常重新起算后的到期日，
    基础窗口尚未出现异常时也允许登记到期日后 14 天内的新情况（出现异常即顺延）。
    """
    exposure_end = last_exposure_day(data)
    start, due = observation_window(exposure_end)
    stored_start = data.get("followup_start")
    stored_due = data.get("due_at")
    if stored_start:
        start = parse_day(stored_start, "followup_start")
    if stored_due:
        due = parse_day(stored_due, "due_at")
    followups = data.get("followups") or {}
    abnormal_days = [
        parse_day(day, "followup_date")
        for day, record in followups.items()
        if record.get("symptoms")
    ]
    if abnormal_days:
        due = max(due, max(abnormal_days) + timedelta(days=FOLLOWUP_DAYS))
    else:
        due = due + timedelta(days=FOLLOWUP_DAYS)
    return start, due


def evaluate_release(data, as_of=None):
    """
    评估接触者是否可以解除医学观察。

    规则：
    - 观察期从最后接触日次日起算 14 天；
    - 最近一次出现发热、咳嗽等症状的随访为异常，解除日期从异常次日重新起算 14 天；
    - 截至拟解除日观察期未满，或缺任何一天有效（无症状）记录，均不可解除。
    """
    exposure_end = last_exposure_day(data)
    window_start, base_due = observation_window(exposure_end)

    followups = data.get("followups") or {}
    abnormal_days = sorted(
        parse_day(day, "followup_date")
        for day, record in followups.items()
        if day >= to_iso(window_start) and record.get("symptoms")
    )
    last_abnormal = abnormal_days[-1] if abnormal_days else None
    effective_due = base_due
    if last_abnormal:
        effective_due = max(base_due, last_abnormal + timedelta(days=FOLLOWUP_DAYS))
    required_start = effective_due - timedelta(days=FOLLOWUP_DAYS - 1)

    as_of_day = parse_day(as_of, "as_of") if as_of else date.today()

    missing = []
    for offset in range(FOLLOWUP_DAYS):
        day = required_start + timedelta(days=offset)
        record = followups.get(to_iso(day))
        if not record or record.get("symptoms"):
            missing.append(to_iso(day))

    reasons = []
    if as_of_day < effective_due:
        reasons.append(
            "观察期未满：最早可解除日期为 %s（截至 %s 仍有 %d 天）"
            % (to_iso(effective_due), to_iso(as_of_day), (effective_due - as_of_day).days)
        )
    if missing:
        reasons.append(
            "缺少 %d 天有效（无症状）随访记录：%s"
            % (len(missing), "、".join(missing))
        )

    streak_start = (last_abnormal + timedelta(days=1)) if last_abnormal else window_start
    streak = 0
    cursor = streak_start
    upper = min(as_of_day, effective_due)
    while cursor <= upper:
        record = followups.get(to_iso(cursor))
        if not record or record.get("symptoms"):
            break
        streak += 1
        cursor += timedelta(days=1)

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "last_exposure_date": to_iso(exposure_end),
        "window_start": to_iso(window_start),
        "due_at": to_iso(base_due),
        "last_abnormal_date": to_iso(last_abnormal) if last_abnormal else None,
        "effective_due_at": to_iso(effective_due),
        "required_range": [to_iso(required_start), to_iso(effective_due)],
        "missing_dates": missing,
        "consecutive_asymptomatic_days": streak,
    }


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_contact(actor, data, lookup):
    start = parse_day(data.get("exposure_start"), "exposure_start")
    if data.get("exposure_end"):
        end = parse_day(data.get("exposure_end"), "exposure_end")
        if end < start:
            raise ValidationError("exposure_end 不能早于 exposure_start")
    else:
        data["exposure_end"] = to_iso(start)
    case = _find_one(lookup, "case", "id", data.get("case_id"))
    if not case:
        raise ValidationError("关联病例不存在: " + str(data.get("case_id")))


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _begin_followup(actor, entity, data, lookup):
    # 观察窗口始终按最后接触日计算，忽略调用方传入的过期日期
    start, due = observation_window(last_exposure_day(entity["data"]))
    return {"followup_start": to_iso(start), "due_at": to_iso(due)}


def _record_followup(actor, entity, data, lookup):
    day = parse_day(data.get("followup_date"), "followup_date")
    key = to_iso(day)
    followups = dict(entity["data"].get("followups") or {})
    if key in followups:
        raise ConflictError(
            "%s 已存在随访记录，一人一天只收一份；如填错请使用 revise_followup 订正" % key
        )
    start, due = _current_window(entity["data"])
    if day < start or day > due:
        raise ValidationError(
            "随访日期 %s 不在当前观察期 %s 至 %s 内" % (key, to_iso(start), to_iso(due))
        )
    symptoms = normalize_symptoms(data.get("symptoms"))
    followups[key] = {
        "followup_date": key,
        "symptoms": symptoms,
        "symptom_labels": [SYMPTOM_LABELS.get(item, item) for item in symptoms],
        "note": data.get("note") or "",
        "recorded_by": actor.user_id,
        "recorded_at": stamp(),
        "revisions": [],
    }
    return {"followups": followups}


def _revise_followup(actor, entity, data, lookup):
    day = parse_day(data.get("followup_date"), "followup_date")
    key = to_iso(day)
    followups = dict(entity["data"].get("followups") or {})
    current = followups.get(key)
    if not current:
        raise ValidationError("%s 尚无随访记录，无法订正，请先登记" % key)
    symptoms = normalize_symptoms(data.get("symptoms"))
    revised = dict(current)
    revised["revisions"] = list(current.get("revisions", [])) + [{
        "symptoms": current.get("symptoms", []),
        "symptom_labels": list(current.get("symptom_labels", [])),
        "note": current.get("note", ""),
        "reason": data.get("reason"),
        "revised_by": actor.user_id,
        "revised_at": stamp(),
    }]
    revised["symptoms"] = symptoms
    revised["symptom_labels"] = [SYMPTOM_LABELS.get(item, item) for item in symptoms]
    if data.get("note") is not None:
        revised["note"] = data.get("note")
    followups[key] = revised
    return {"followups": followups}


def _complete_followup(actor, entity, data, lookup):
    as_of = data.get("as_of") or data.get("released_at")
    result = evaluate_release(entity["data"], as_of)
    if not result["eligible"]:
        raise ValidationError("暂不能解除医学观察：" + "；".join(result["reasons"]))
    release_day = to_iso(parse_day(as_of)) if as_of else result["effective_due_at"]
    return {
        "outcome": data.get("outcome") or "观察期满，连续14天无发热、咳嗽等症状",
        "completed_at": release_day,
        "release_evaluation": {
            "effective_due_at": result["effective_due_at"],
            "last_abnormal_date": result["last_abnormal_date"],
            "consecutive_asymptomatic_days": result["consecutive_asymptomatic_days"],
        },
    }


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case, 'contact': _validate_contact}
CUSTOM_TRANSITIONS = {
    ('case', 'lab_positive'): _validate_lab_positive,
    ('case', 'mark_probable'): _validate_probable,
    ('contact', 'begin_followup'): _begin_followup,
    ('contact', 'record_followup'): _record_followup,
    ('contact', 'revise_followup'): _revise_followup,
    ('contact', 'complete_followup'): _complete_followup,
}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {
        'case': {
            'triage': (('reported',), 'investigating'),
            'lab_positive': (('investigating',), 'confirmed'),
            'mark_probable': (('investigating',), 'probable'),
            'recover': (('confirmed', 'probable'), 'recovered'),
            'close': (('recovered',), 'closed'),
        },
        'contact': {
            'begin_followup': (('identified', 'completed'), 'following'),
            'record_followup': (('following',), 'following'),
            'revise_followup': (('following',), 'following'),
            'complete_followup': (('following',), 'completed'),
        },
    }
    CREATE_REQUIRED = {
        'case': ('person_id', 'onset_date', 'location', 'symptoms'),
        'contact': ('case_id', 'person_id', 'exposure_start'),
    }
    ACTION_REQUIRED = {
        ('case', 'triage'): ('clinician',),
        ('case', 'lab_positive'): ('lab_id', 'result'),
        ('case', 'mark_probable'): ('epi_link',),
        ('case', 'recover'): ('recovered_at',),
        ('case', 'close'): ('outcome',),
        ('contact', 'record_followup'): ('followup_date',),
        ('contact', 'revise_followup'): ('followup_date', 'reason'),
    }
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {
        'triage': ('admin', 'clinician'),
        'lab_positive': ('admin', 'lab'),
        'mark_probable': ('admin', 'investigator'),
        'recover': ('admin', 'clinician'),
        'close': ('admin', 'investigator'),
        'begin_followup': ('admin', 'investigator'),
        'record_followup': ('admin', 'investigator'),
        'revise_followup': ('admin', 'investigator'),
        'complete_followup': ('admin', 'investigator'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        if custom:
            # 自定义动作显式声明要落库的字段，临时入参（如 followup_date）不进入实体
            patch = dict(custom(actor, entity, data, lookup) or {})
        else:
            patch = dict(data)
        return next_status, patch
