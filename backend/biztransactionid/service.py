import requests
import json
import base64
import gzip
from config import settings


# ============================================================
# Build Criteria
# ============================================================

def build_criteria(transaction_class_id: int, login: dict) -> dict:
    """
    Build criteria dynamically using Login DTO
    """

    work_ou_id = login.get("WorkOUId")
    work_period_id = login.get("WorkPeriodId")

    if not work_ou_id or not work_period_id:
        raise ValueError("WorkOUId or WorkPeriodId missing in login")

    return {
        "SectionCriteriaList": [
            {
                "SectionId": 0,
                "AttributesCriteriaList": [
                    {
                        "FieldName": "BIZTransactionTypeClassId",
                        "OperationType": 1,
                        "FieldValue": transaction_class_id,
                        "InArray": None,
                        "JoinType": 2
                    },
                    {
                        "FieldName": "OrganizationUnit.Id",
                        "OperationType": 1,
                        "FieldValue": work_ou_id,
                        "InArray": None,
                        "JoinType": 2
                    },
                    {
                        "FieldName": "Period.Id",
                        "OperationType": 1,
                        "FieldValue": work_period_id,
                        "InArray": None,
                        "JoinType": 0
                    }
                ],
                "OperationType": 0
            }
        ]
    }


# ============================================================
# Decode ADS gzip + base64 response
# ============================================================

def decode_response_body(body_base64: str) -> dict:
    try:
        compressed_data = base64.b64decode(body_base64)
        decompressed_data = gzip.decompress(compressed_data)
        return json.loads(decompressed_data.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to decode response body: {str(e)}")


# ============================================================
# Get BizTransactionTypeId (ADS + Normal support)
# ============================================================

def get_biz_transaction_type_id(transaction_class_id: int, login: dict) -> int:
    try:
        # Use login object to get correct server URL
        base_url = settings.get_direct_url(login)
        url = f"{base_url}/ads/BizTransactionType.svc/SelectList"

        headers = {
            "Content-Type": "application/json",
            "Login": json.dumps(login)
        }

        payload = build_criteria(transaction_class_id, login)

        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        data = response.json()

        # ==========================================================
        # Case 1: New GoodBooks JSON response
        # ==========================================================
        contents = data.get("contents")
        if isinstance(contents, list) and len(contents) > 0:
            biz_id = contents[0].get("BizTransactionTypeId")
            if biz_id:
                return int(biz_id)

        # ==========================================================
        # Case 2: ADS Gateway response inside contents
        # ==========================================================
        if isinstance(contents, dict) and "Body" in contents:
            decoded = decode_response_body(contents["Body"])
            body_json = decoded.get("Body")
            rows = json.loads(body_json)
            return int(rows[0]["Id"])

        # ==========================================================
        # ✅ Case 3: DIRECT ADS RESPONSE (YOUR CASE)
        # ==========================================================
        if "Body" in data and isinstance(data["Body"], str):
            decoded = decode_response_body(data["Body"])
            body_json = decoded.get("Body")

            if not body_json:
                raise Exception("ADS body empty")

            rows = json.loads(body_json)
            return int(rows[0]["Id"])

        raise Exception("BizTransactionTypeId not found in any response format")

    except Exception as e:
        raise Exception(f"Failed to get BizTransactionTypeId: {str(e)}")
