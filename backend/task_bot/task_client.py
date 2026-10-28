import json
import re
import uuid
import calendar
import gzip
import base64
import time
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional
import logging

from config import settings
from biztransactionid.service import get_biz_transaction_type_id
from biztransactionid import TASK_TRANSACTION_CLASS_ID

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({
    'User-Agent': 'TaskClient/1.0',
    'Accept': 'application/json'
})

try:
    with open("criteria.json", "r", encoding="utf-8") as f:
        CRITERIA = json.load(f)
    PARENT_TASK_HOURS_CRITERIA = CRITERIA.get("PARENT_TASK_HOURS_CRITERIA", {})
    ACTIVITY_LIST = CRITERIA.get("ACTIVITY_LIST", [])
    logger.info("✓ Criteria loaded successfully from criteria.json")
except FileNotFoundError:
    logger.error("❌ criteria.json file not found! Task functionality will be limited.")
    PARENT_TASK_HOURS_CRITERIA = {}
    ACTIVITY_LIST = []
except (KeyError, json.JSONDecodeError) as e:
    logger.error(f"❌ Problem reading criteria.json: {e}. Task functionality will be limited.")
    PARENT_TASK_HOURS_CRITERIA = {}
    ACTIVITY_LIST = []


def direct_url(path: str, login: Dict[str, Any] = None) -> str:
    base = settings.get_direct_url(login)
    path = path.lstrip("/")
    url = f"{base}/{path}"
    logger.debug(f"Direct URL: {url}")
    return url


def clean_section_criteria(section_list: List[Dict]) -> List[Dict]:
    cleaned = []
    for section in section_list:
        attrs = []
        for attribute in section.get("AttributesCriteriaList", []):
            field_value = attribute.get("FieldValue")
            if field_value is None:
                continue
            if isinstance(field_value, str) and field_value.strip() == "":
                continue
            attrs.append(attribute)
        cleaned_section = dict(section)
        cleaned_section["AttributesCriteriaList"] = attrs
        cleaned.append(cleaned_section)
    return cleaned


def decode_response_body(body_base64: str) -> Dict[str, Any]:
    try:
        compressed = base64.b64decode(body_base64)
        decompressed = gzip.decompress(compressed)
        decoded_str = decompressed.decode("utf-8")
        return json.loads(decoded_str)
    except Exception as e:
        logger.error(f"Decode error: {e}")
        return {}


def parse_api_response(response_data: Dict[str, Any]) -> Any:
    status = response_data.get("Status")
    if status and status != 200:
        logger.warning(f"⚠️ API returned non-200 status: {status}")

    def check_decoded_error(decoded):
        if "ErrorNumber" in decoded:
            error_msg = decoded.get('Body', 'Unknown error')
            logger.error(f"❌ API Error: {error_msg}")
            return True
        return False

    contents = response_data.get("contents", {})
    if "Body" in contents:
        body = contents["Body"]
        if isinstance(body, str) and body:
            decoded = decode_response_body(body)
            if check_decoded_error(decoded):
                return []
            if "Body" in decoded:
                inner = decoded["Body"]
                return json.loads(inner) if isinstance(inner, str) else inner
            if "ResponseObject" in decoded:
                return decoded["ResponseObject"]

    if "Body" in response_data:
        body = response_data["Body"]
        if isinstance(body, str) and body:
            decoded = decode_response_body(body)
            if check_decoded_error(decoded):
                return []
            if "Body" in decoded:
                inner = decoded["Body"]
                return json.loads(inner) if isinstance(inner, str) else inner
            if "ResponseObject" in decoded:
                return decoded["ResponseObject"]

    if "ResponseObject" in response_data:
        return response_data["ResponseObject"]
    if "Data" in response_data:
        return response_data["Data"]

    return []


def _parse_date(val: str) -> Optional[datetime]:
    if not val:
        return None
    match = re.search(r"\d{2}[-/]\d{2}[-/]\d{2,4}", val)
    if not match:
        return None
    clean = match.group(0).replace("/", "-")
    for fmt in ("%d-%m-%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None


def _gb_date(dt: datetime) -> str:
    timestamp_ms = calendar.timegm(dt.timetuple()) * 1000
    return f"/Date({timestamp_ms})/"


def _gb_unix_seconds(dt: datetime) -> int:
    return calendar.timegm(dt.timetuple())


def _parse_hhmm_to_minutes(val: str) -> Optional[int]:
    if not val:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", val.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    return hours * 60 + minutes


def _minutes_to_hhmm(total_minutes: int) -> str:
    hours, minutes = divmod(max(total_minutes, 0), 60)
    return f"{hours:02d}:{minutes:02d}"


MAX_HOURS_PER_TASK_MIN = 8 * 60
MAX_HOURS_PER_DAY_MIN = 8 * 60


TASK_PICKLIST_SEARCH_CRITERIA_TEMPLATE = {
    "SectionCriteriaList": [
        {
            "SectionId": 0,
            "AttributesCriteriaList": [
                {"FieldName": "TaskOriginalStatus", "OperationType": 7,
                 "FieldValue": "4", "InArray": None, "JoinType": 2},
                {"FieldName": "Status", "OperationType": 5,
                 "FieldValue": "1", "InArray": None, "JoinType": 2},
                {"FieldName": "DetailType", "OperationType": 5,
                 "FieldValue": "3", "InArray": None, "JoinType": 2},
                {"FieldName": "Number", "OperationType": 2,
                 "FieldValue": "", "InArray": None, "JoinType": 1},
                {"FieldName": "TaskName", "OperationType": 2,
                 "FieldValue": "", "InArray": None, "JoinType": 0},
            ],
            "OperationType": 0,
        }
    ]
}


def validate_work_number(work_number: str, login: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"🔍 Validating Work Number: {work_number}")
    try:
        if not re.match(r"^W\d+$", work_number.strip()):
            return {"valid": False, "reason": f"'{work_number}' doesn't look like a valid "
                                               f"Work Number (expected format: W0092618)."}

        url = direct_url(
            "/crm/Task.svc/GetTaskPicklistSelect2/?FirstNumber=1&MaxResult=50", login
        )

        import copy
        payload = copy.deepcopy(TASK_PICKLIST_SEARCH_CRITERIA_TEMPLATE)
        for section in payload.get("SectionCriteriaList", []):
            for attr in section.get("AttributesCriteriaList", []):
                if attr.get("FieldName") in ("Number", "TaskName"):
                    attr["FieldValue"] = work_number.strip()
        payload["SectionCriteriaList"] = clean_section_criteria(
            payload["SectionCriteriaList"]
        )

        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
        rows = result if isinstance(result, list) else []

        match = next(
            (r for r in rows if str(r.get("Number", "")).strip().upper()
             == work_number.strip().upper()),
            None
        )

        if not match:
            return {"valid": False, "reason": f"Work Number {work_number} was not found."}

        return {
            "valid": True,
            "requester": match.get("Requester", login.get("UserName", "")),
            "allocation_split": match.get("AllocationSplit", ""),
            "work_id": match.get("Id"),
            "parent_task_name": match.get("TaskName", ""),
        }

    except Exception as e:
        logger.error(f"❌ Error validating Work Number {work_number}: {e}")
        return {"valid": False, "reason": "Couldn't reach the server to validate that Work "
                                           "Number. Please try again."}


ACTIVITY_SEARCH_CRITERIA_TEMPLATE = {
    "SectionCriteriaList": [
        {
            "SectionId": 0,
            "AttributesCriteriaList": [
                {"FieldName": "Code", "OperationType": 2,
                 "FieldValue": "", "InArray": None, "JoinType": 1},
                {"FieldName": "Name", "OperationType": 2,
                 "FieldValue": "", "InArray": None, "JoinType": 0},
            ],
            "OperationType": 0,
        }
    ]
}


def get_activities(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    logger.info("🔍 Fetching activity list from API...")
    try:
        url = direct_url("/ms/Activity.svc/SelectList", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        response = session.post(
            url, json=ACTIVITY_SEARCH_CRITERIA_TEMPLATE, headers=headers, timeout=60
        )
        response.raise_for_status()

        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []

    except Exception as e:
        logger.error(f"❌ Error fetching activity list: {e}")
        return []


def resolve_activity_id(activity_name: str, login: Dict[str, Any]) -> int:
    name = (activity_name or "").strip()
    if not name or name.upper() == "NONE":
        return -1

    activities = get_activities(login)
    for row in activities:
        if str(row.get("Name", "")).strip().lower() == name.lower():
            return int(row.get("Id"))

    logger.warning(f"⚠️ Activity '{activity_name}' not found in live list, defaulting to NONE")
    return -1


MAX_HOURS_PER_WORK_MIN = 25 * 60


def get_work_hours_used(work_id, login: Dict[str, Any]) -> int:
    logger.info(f"🔍 Fetching hours already used on Work id={work_id}")
    try:
        url = direct_url("/crm/Task.svc/GetParentTaskPlannedHours", login)

        payload = {}
        if "SectionCriteriaList" in PARENT_TASK_HOURS_CRITERIA:
            import copy
            payload = copy.deepcopy(PARENT_TASK_HOURS_CRITERIA)
            for section in payload.get("SectionCriteriaList", []):
                for attr in section.get("AttributesCriteriaList", []):
                    if attr.get("FieldName") == "ParentId":
                        attr["FieldValue"] = work_id
            payload["SectionCriteriaList"] = clean_section_criteria(
                payload["SectionCriteriaList"]
            )

        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
        if isinstance(result, list):
            return sum(int(r.get("TaskPlannedHours", 0)) for r in result)
        if isinstance(result, dict):
            return int(result.get("TaskPlannedHours", 0))
        return 0

    except Exception as e:
        logger.error(f"❌ Error fetching Work hours used: {e}")
        return 0


def get_hours_used_today(login: Dict[str, Any], date_dt: datetime) -> int:
    logger.info(f"🔍 Fetching hours already logged for {date_dt.strftime('%d-%m-%Y')}")
    try:
        url = direct_url(
            "/crm/Task.svc/GetTaskReport/?FirstNumber=-1&MaxResult=-1", login
        )

        day_start = _gb_unix_seconds(date_dt)
        day_end = _gb_unix_seconds(date_dt) + 86399

        payload = {
            "SectionCriteriaList": [
                {
                    "SectionId": 0,
                    "AttributesCriteriaList": [
                        {"FieldName": "OUId", "FieldValue": login.get("WorkOUId")},
                        {"FieldName": "AssigneeId", "FieldValue": login.get("UserId")},
                        {"FieldName": "PeriodFromDate", "FieldValue": day_start},
                        {"FieldName": "PeriodToDate", "FieldValue": day_end},
                    ],
                }
            ]
        }

        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = parse_api_response(response.json())
        rows = result if isinstance(result, list) else []

        total_minutes = sum(int(r.get("TaskPlannedHours", 0)) for r in rows)
        logger.info(f"📊 Hours already logged for {date_dt.strftime('%d-%m-%Y')}: "
                    f"{_minutes_to_hhmm(total_minutes)}")
        return total_minutes

    except Exception as e:
        logger.error(f"❌ Error fetching today's logged hours: {e}")
        raise


def check_daily_hours_available(login: Dict[str, Any], date_dt: datetime,
                                 new_task_minutes: int) -> Dict[str, Any]:
    used = get_hours_used_today(login, date_dt)
    remaining = MAX_HOURS_PER_DAY_MIN - used
    if new_task_minutes > remaining:
        return {
            "ok": False,
            "reason": (
                f"You've already logged {_minutes_to_hhmm(used)} on "
                f"{date_dt.strftime('%d-%m-%Y')}, so only {_minutes_to_hhmm(max(remaining, 0))} "
                f"is left for that day. This task needs {_minutes_to_hhmm(new_task_minutes)}."
            ),
        }
    return {"ok": True}


def _decode_body_field(raw) -> Dict[str, Any]:
    if not isinstance(raw, str):
        return raw if isinstance(raw, dict) else {}
    try:
        decompressed = gzip.decompress(base64.b64decode(raw))
        return json.loads(decompressed.decode("utf-8"))
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return {"Body": raw}


def _fully_unwrap_body(raw, max_depth: int = 5) -> str:
    """
    GB4 sometimes nests the response body multiple levels deep — a "Body"
    string that, once decoded (gzip+base64 or plain JSON), contains ANOTHER
    "Body" string, and so on, before finally reaching the plain-text message
    (e.g. "Details Saved Successfully with Number T0092984 : Id=-1499726643").
    _decode_body_field() only unwraps one layer, which is why a doubly-nested
    response was leaving literal {"Body":...} text in the final message
    instead of the plain sentence. This keeps unwrapping until it hits real
    plain text (or a safety depth limit), regardless of how many layers deep
    it goes.
    """
    current = raw
    for _ in range(max_depth):
        if not isinstance(current, str):
            return str(current)
        decoded = _decode_body_field(current)
        if isinstance(decoded, dict) and isinstance(decoded.get("Body"), str) \
                and decoded["Body"] != current:
            current = decoded["Body"]
            continue
        if isinstance(decoded, dict) and "Body" in decoded:
            return str(decoded["Body"])
        return current
    return current if isinstance(current, str) else str(current)


# AllocationId/AllocationSplitId are hardcoded from the one confirmed-working capture
# (GB4.5 / DEVELOPMENT). Needs a real lookup if this org uses more than one of either.
_CONFIRMED_ALLOCATION_ID = "-1499999126"
_CONFIRMED_ALLOCATION_NAME = "GB4.5"
_CONFIRMED_ALLOCATION_SPLIT_ID = "-1499999906"
_CONFIRMED_ALLOCATION_SPLIT_NAME = "DEVELOPMENT"


def create_task(slots: Dict[str, Any], login: Dict[str, Any],
                 requester: str, allocation_split: str, work_id,
                 parent_task_name: str = "") -> Dict[str, Any]:
    logger.info("📝 Creating task...")
    try:
        task_dt = _parse_date(slots.get("TaskDate"))
        if not task_dt:
            raise Exception("Invalid task date provided")

        hours_minutes = _parse_hhmm_to_minutes(slots.get("Hours"))
        if hours_minutes is None:
            raise Exception("Invalid Hours format — expected HH:MM, e.g. 08:00")
        if hours_minutes > MAX_HOURS_PER_TASK_MIN:
            raise Exception(f"Hours can't exceed 08:00 per task "
                             f"(got {slots.get('Hours')})")

        plan_days = int(slots.get("PlanDays") or 1)
        project_sequence = int(slots.get("ProjectSequence") or 0)
        resource_sequence = int(slots.get("ResourceSequence") or 0)
        if project_sequence <= 0:
            raise Exception("Project Sequence is required")
        if resource_sequence <= 0:
            raise Exception("Resource Sequence is required")

        activity_name = slots.get("Activity", "NONE") or "NONE"
        activity_id = resolve_activity_id(activity_name, login)

        biz_type_id = str(get_biz_transaction_type_id(TASK_TRANSACTION_CLASS_ID, login))
        emp_id = str(login["UserId"])
        emp_name = login.get("UserName", "")
        same_date = _gb_date(task_dt)

        def _safe_int(val, default=0):
            try:
                return int(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        payload = {
            "TaskId": 0,
            "TaskTaskType": 0,
            "BizTransactionTypeId": _safe_int(biz_type_id),
            "OuId": _safe_int(login.get("WorkOUId")),
            "OuName": login.get("OuName", ""),
            "PeriodId": _safe_int(login.get("WorkPeriodId")),
            "TaskStatus": 1,
            "TaskNumber": "",
            "TaskDate": same_date,
            "TaskReferenceNumber": "0",
            "TaskReferenceDate": same_date,
            "TaskTaskName": slots.get("TaskName", ""),
            "TaskDescription": slots.get("Description", ""),
            "TaskDetailType": 4,
            "TaskTaskNature": 5,
            "PartyBranchId": -1,
            "AssignedToContactId": -1,
            "AssetId": -1,
            "AssetName": "",
            "ActivityId": _safe_int(activity_id, -1),
            "ActivityName": activity_name,
            "RequestedById": _safe_int(emp_id),
            "RequestedByName": emp_name,
            "InChargeId": _safe_int(emp_id),
            "InChargeName": emp_name,
            "AllocationId": _safe_int(_CONFIRMED_ALLOCATION_ID),
            "AllocationName": _CONFIRMED_ALLOCATION_NAME,
            "TaskWorkType": -1,
            "TaskSlNo": 1,
            "TaskWbcCode": "",
            "TaskWbcLevel": 0,
            "TaskAssignedToType": 4,
            "AssignedToRoleId": -1,
            "AssignedToUserId": -1,
            "AssignedToUserGroupId": -1,
            "TaskOriginalStatus": 0,
            "TaskTags": 0,
            "TaskPlannedDays": plan_days,
            "TaskPlannedHours": hours_minutes,
            "TaskPlannedMinutesInHours": slots.get("Hours", ""),
            "TaskPlanStartOn": same_date,
            "TaskPlanEndOn": same_date,
            "TaskDueOn": same_date,
            "TaskExpiryTime": 0,
            "TaskExpireOn": same_date,
            "TaskGraceTime": 0,
            "TaskComplexity": 0,
            "TaskCriticality": 0,
            "TaskPriority": 0,
            "TaskActualStartOn": same_date,
            "TaskActualEndOn": same_date,
            "TaskActualDays": "0",
            "TaskActualHours": 0,
            "TaskCompletedPercentage": "0",
            "HeaderTaskId": -1,
            "ParentTaskId": _safe_int(work_id),
            "ParentTaskTaskName": parent_task_name,
            "ParentTaskNumber": slots.get("WorkNumber", ""),
            "PredecessorId": -1,
            "ResourceId": -1,
            "ResourceName": "",
            "MachineId": -1,
            "MachineName": "",
            "CallId": -1,
            "CallNumber": "",
            "AssignedToEmployeeId": _safe_int(emp_id),
            "AssignedToEmployeeName": emp_name,
            "AssignedToWorkTypeId": -1,
            "InChargePrimaryEMailId": login.get("PrimaryEMailId", ""),
            "InChargeCode": login.get("EmployeeCode", ""),
            "InChargeDefaultImage": "",
            "AssignedToEmployeeDefaultImage": "",
            "AssignedToEmployeePrimaryEMailId": "",
            "AssignedToEmployeeCode": login.get("EmployeeCode", ""),
            "TaskWorkFlowRequired": 0,
            "TaskIsToDo": 0,
            "TaskRollType": 0,
            "TaskBudgetCost": "0",
            "TaskBudgetRevenue": "0",
            "TaskActualCost": "0",
            "LeadId": -1,
            "LeadName": "",
            "VehicleId": -1,
            "VehicleNumber": "",
            "TaskVersion": 1,
            "AllocationSplitId": _safe_int(_CONFIRMED_ALLOCATION_SPLIT_ID),
            "SplitName": _CONFIRMED_ALLOCATION_SPLIT_NAME,
            "TaskProjectSequence": project_sequence,
            "TaskResourceSequence": resource_sequence,
            "TaskIsBillable": 0,
            "TaskIsReWork": 1,
        }

        logger.info(f"🚀 Submitting Task Payload: {json.dumps([payload], indent=2)}")

        url = direct_url("/crm/Task.svc/", login)
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}

        response = session.post(url, json=[payload], headers=headers, timeout=60)
        if not response.ok:
            try:
                error_body = response.text
            except Exception:
                error_body = "(no body)"
            logger.error(f"❌ GB4 Task.svc returned {response.status_code}: {error_body}")
            raise Exception(f"{response.status_code} {response.reason} — GB4 error: {error_body[:500]}")
        response.raise_for_status()

        result = response.json()
        logger.info(f"📩 API Response: {json.dumps(result, indent=2)}")

        status = result.get("Status") if isinstance(result, dict) else None
        if status and status not in (200,):
            decoded = _decode_body_field(result.get("Body", ""))
            raise Exception(f"API Rejected Request: {decoded.get('Body', decoded)}")

        plain_text = _fully_unwrap_body(result.get("Body", "")) if isinstance(result, dict) else ""
        number_match = re.search(r"Number\s+([A-Za-z0-9]+)\s*:", plain_text)
        id_match = re.search(r"Id\s*=\s*(-?\d+)", plain_text)

        task_number = number_match.group(1) if number_match else ""
        task_id = id_match.group(1) if id_match else (
            result.get("Id") if isinstance(result, dict) else None
        )
        work_number = slots.get("WorkNumber", "")

        if task_number:
            message = f"Task created successfully with Number {task_number} for the Work Number {work_number}."
        elif plain_text:
            message = plain_text
        else:
            message = "Task created successfully."

        logger.info("✅ Task created successfully")
        return {
            "success": True,
            "task_id": task_id,
            "task_number": task_number,
            "message": message,
        }

    except Exception as e:
        logger.error(f"❌ Task creation failed: {e}")
        raise


if __name__ == "__main__":
    print("Task Client Module Loaded")