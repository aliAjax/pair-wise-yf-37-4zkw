import re
from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 医学观察天数：自最后接触日（或最近一次异常）起连续14天无症状方可解除
OBSERVATION_DAYS = 14
# 体温达到该值即视为异常（发热）
FEVER_TEMPERATURE = 37.3
SYMPTOM_SPLIT = re.compile(r"[,，、;；\s]+")


def parse_day(value):
    """Parse a YYYY-MM-DD value into a date."""
    if value is None or str(value).strip() == "":
        raise ValidationError("date is required")
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        raise ValidationError("invalid date: %s (expected YYYY-MM-DD)" % value)


def to_day(value):
    return value.isoformat()


def normalize_symptoms(value):
    """Normalize symptoms into a de-duplicated list of non-empty strings."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [part for part in SYMPTOM_SPLIT.split(value) if part]
    if not isinstance(value, list):
        raise ValidationError("symptoms must be a list")
    result = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def parse_temperature(value):
    if value is None or value == "":
        return None
    try:
        temperature = round(float(value), 1)
    except (TypeError, ValueError):
        raise ValidationError("temperature must be a number")
    if not 34.0 <= temperature <= 43.0:
        raise ValidationError("temperature out of plausible range: %s" % temperature)
    return temperature


def is_abnormal(symptoms, temperature=None):
    """Any reported symptom (fever, cough, ...) or high temperature is abnormal."""
    return bool(symptoms) or (temperature is not None and temperature >= FEVER_TEMPERATURE)


def last_contact_date(contact_data):
    """最后接触日：优先 exposure_end，缺省回退 exposure_start。"""
    raw = contact_data.get("exposure_end") or contact_data.get("exposure_start")
    return parse_day(raw)


def observation_due(anchor):
    return anchor + timedelta(days=OBSERVATION_DAYS)


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_contact(actor, data, lookup):
    start = parse_day(data.get("exposure_start"))
    exposure_end = data.get("exposure_end")
    if exposure_end:
        end = parse_day(exposure_end)
        if end < start:
            raise ValidationError("exposure_end cannot be earlier than exposure_start")
    if lookup:
        linked = lookup("case", "id", data.get("case_id")) or []
        if not linked:
            raise ValidationError("case_id does not reference an existing case")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def _validate_begin_followup(actor, entity, data, lookup):
    # 观察窗一律按最后接触日推算14天，忽略调用方自行填写的 due_at
    last_contact = last_contact_date(entity["data"])
    return {
        "last_contact_date": to_day(last_contact),
        "due_at": to_day(observation_due(last_contact)),
    }


def _build_followup_patch(data):
    day = parse_day(data.get("followup_date"))
    if day > date.today():
        raise ValidationError("followup_date cannot be later than today")
    symptoms = normalize_symptoms(data.get("symptoms"))
    temperature = parse_temperature(data.get("temperature"))
    return {
        "followup_date": to_day(day),
        "symptoms": symptoms,
        "temperature": temperature,
        "abnormal": is_abnormal(symptoms, temperature),
    }


def _validate_record_followup(actor, entity, data, lookup):
    return _build_followup_patch(data)


def _validate_amend_followup(actor, entity, data, lookup):
    return _build_followup_patch(data)


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


def evaluate_release(contact_data, followups, as_of=None):
    """评估接触者是否可以解除观察。

    规则：
    - 起算锚点 = max(最后接触日, 最近一次异常随访日)；
    - 锚点之后连续 OBSERVATION_DAYS 天每天都有一份无症状（无发热、咳嗽等）
      的有效随访记录；
    - 评估日早于锚点+14天 -> 观察期未满；
    - 期满但窗口内有日期缺记录 -> 缺少有效随访记录。
    """
    as_of_day = parse_day(as_of) if as_of else date.today()
    last_contact = last_contact_date(contact_data)

    latest_abnormal = None
    by_day = {}
    for item in followups or []:
        day = parse_day(item.get("followup_date"))
        if day > as_of_day:
            continue
        by_day[day] = item
        if item.get("abnormal"):
            latest_abnormal = max(latest_abnormal, day) if latest_abnormal else day

    anchor = max(last_contact, latest_abnormal) if latest_abnormal else last_contact
    earliest = observation_due(anchor)
    window_days = [anchor + timedelta(days=offset) for offset in range(1, OBSERVATION_DAYS + 1)]
    missing = [to_day(day) for day in window_days if day <= as_of_day and day not in by_day]

    result = {
        "eligible": False,
        "as_of": to_day(as_of_day),
        "last_contact_date": to_day(last_contact),
        "latest_abnormal_date": to_day(latest_abnormal) if latest_abnormal else None,
        "anchor_date": to_day(anchor),
        "earliest_release_date": to_day(earliest),
        "observation_days": OBSERVATION_DAYS,
        "missing_dates": missing,
        "reason": None,
    }

    if as_of_day < earliest:
        if latest_abnormal and latest_abnormal >= last_contact:
            result["reason"] = (
                "观察期未满：最近一次异常随访为 %s，需自次日起连续%d天无发热、咳嗽等症状，"
                "最早可解除日期为 %s（当前评估日 %s）。"
                % (to_day(latest_abnormal), OBSERVATION_DAYS, to_day(earliest), to_day(as_of_day))
            )
        else:
            result["reason"] = (
                "观察期未满：最后接触日为 %s，需连续%d天无发热、咳嗽等症状，"
                "最早可解除日期为 %s（当前评估日 %s）。"
                % (to_day(last_contact), OBSERVATION_DAYS, to_day(earliest), to_day(as_of_day))
            )
        return result
    if missing:
        result["reason"] = (
            "缺少有效随访记录：%s 等%d天未提交随访，一人一天须有一份无症状记录，补齐后再解除。"
            % ("、".join(missing[:5]), len(missing))
        )
        return result
    result["eligible"] = True
    return result


CUSTOM_CREATE = {'case': _validate_case, 'contact': _validate_contact}
CUSTOM_TRANSITIONS = {
    ('case', 'lab_positive'): _validate_lab_positive,
    ('case', 'mark_probable'): _validate_probable,
    ('contact', 'begin_followup'): _validate_begin_followup,
    ('contact', 'record_followup'): _validate_record_followup,
    ('contact', 'amend_followup'): _validate_amend_followup,
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
            'begin_followup': (('identified',), 'following'),
            'record_followup': (('following',), 'following'),
            'amend_followup': (('following',), 'following'),
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
        ('contact', 'begin_followup'): ('followup_start',),
        ('contact', 'record_followup'): ('followup_date',),
        ('contact', 'amend_followup'): ('followup_date', 'reason'),
        ('contact', 'complete_followup'): ('outcome',),
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
        'amend_followup': ('admin', 'investigator'),
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
        patch = dict(data)
        if custom:
            extra = custom(actor, entity, data, lookup) or {}
            patch.update(extra)
        return next_status, patch

    # 暴露给 service 层复用的纯规则助手
    parse_day = staticmethod(parse_day)
    to_day = staticmethod(to_day)
    last_contact_date = staticmethod(last_contact_date)
    observation_due = staticmethod(observation_due)
    evaluate_release = staticmethod(evaluate_release)


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
