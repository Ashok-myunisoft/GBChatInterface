"""
Complete Purchase Order Client for GB API
Handles party, item, store fetching and PO creation.
Payload built dynamically — no hardcoded master data IDs.
"""

import json
import re
import uuid
import calendar
import gzip
import base64
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional
import logging

from config import settings
from biztransactionid.service import get_biz_transaction_type_id
from biztransactionid import PURCHASE_TRANSACTION_CLASS_ID


# ============================================================
# Logging Configuration
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# Persistent Session
# ============================================================
session = requests.Session()
session.headers.update({
    'User-Agent': 'PurchaseClient/1.0',
    'Accept': 'application/json'
})


# ============================================================
# Load Criteria from JSON
# ============================================================
try:
    with open("criteria.json", "r", encoding="utf-8") as f:
        CRITERIA = json.load(f)
    
    PARTY_CRITERIA = CRITERIA.get("PARTY_CRITERIA", {})
    ITEM_CRITERIA = CRITERIA.get("ITEM_CRITERIA", {})
    STORE_CRITERIA = CRITERIA.get("STORE_CRITERIA", {})
    logger.info("Criteria loaded successfully from criteria.json")
    
except FileNotFoundError:
    logger.error("criteria.json file not found! Purchase functionality will be limited.")
    PARTY_CRITERIA = {}
    ITEM_CRITERIA = {}
    STORE_CRITERIA = {}

except Exception as e:
    logger.error(f"Error loading criteria.json: {e}. Purchase functionality will be limited.")
    PARTY_CRITERIA = {}
    ITEM_CRITERIA = {}
    STORE_CRITERIA = {}


# ============================================================
# URL Helpers
# ============================================================
def direct_url(path: str, login: Dict[str, Any] = None) -> str:
    """Build direct API URL using login object's BaseUri if available."""
    base = settings.get_direct_url(login)
    path = path.lstrip("/")
    url = f"{base}/{path}"
    logger.debug(f"Direct URL: {url}")
    return url


# ============================================================
# Clean Criteria (Remove Empty Fields)
# ============================================================
def clean_section_criteria(section_list: List[Dict]) -> List[Dict]:
    """Remove empty or null values from criteria to prevent API errors."""
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


# ============================================================
# Response Decoder
# ============================================================
def decode_response_body(body_base64: str) -> Dict[str, Any]:
    """Decode base64 + gzip compressed response body."""
    try:
        compressed = base64.b64decode(body_base64)
        decompressed = gzip.decompress(compressed)
        decoded_str = decompressed.decode("utf-8")
        return json.loads(decoded_str)
    except Exception as e:
        logger.error(f"Decode error: {e}")
        return {}


# ============================================================
# Response Parser
# ============================================================
def parse_api_response(response_data: Dict[str, Any]) -> Any:
    """Parse API response and extract data."""
    
    status = response_data.get("Status")
    if status and status != 200:
        logger.warning(f"API returned non-200 status: {status}")

    def check_decoded_error(decoded):
        if "ErrorNumber" in decoded:
            error_msg = decoded.get('Body', 'Unknown error')
            logger.error(f"API Error: {error_msg}")
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


# ============================================================
# Get Parties
# ============================================================
def get_parties(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch parties (vendors/suppliers) using criteria."""
    logger.info("Fetching parties from API...")
    try:
        url = direct_url("/mms/Party.svc/SelectList", login)
        
        if "SectionCriteriaList" in PARTY_CRITERIA:
            payload = PARTY_CRITERIA.copy()
            payload["SectionCriteriaList"] = clean_section_criteria(
                PARTY_CRITERIA["SectionCriteriaList"]
            )
        else:
            payload = PARTY_CRITERIA
        
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
            
    except Exception as e:
        logger.error(f"Error fetching parties: {e}")
        return []


# ============================================================
# Get Items
# ============================================================
def get_items(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch items using criteria."""
    logger.info("Fetching items from API...")
    try:
        url = direct_url("/mms/Item.svc/SelectList", login)
        
        if "SectionCriteriaList" in ITEM_CRITERIA:
            payload = ITEM_CRITERIA.copy()
            payload["SectionCriteriaList"] = clean_section_criteria(
                ITEM_CRITERIA["SectionCriteriaList"]
            )
        else:
            payload = ITEM_CRITERIA
        
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
            
    except Exception as e:
        logger.error(f"Error fetching items: {e}")
        return []


# ============================================================
# Get Stores
# ============================================================
def get_stores(login: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch stores using criteria."""
    logger.info("Fetching stores from API...")
    try:
        url = direct_url("/mms/Store.svc/SelectList", login)
        
        if "SectionCriteriaList" in STORE_CRITERIA:
            payload = STORE_CRITERIA.copy()
            payload["SectionCriteriaList"] = clean_section_criteria(
                STORE_CRITERIA["SectionCriteriaList"]
            )
        else:
            payload = STORE_CRITERIA
        
        headers = {"Content-Type": "application/json", "Login": json.dumps(login)}
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = parse_api_response(response.json())
        return result if isinstance(result, list) else []
            
    except Exception as e:
        logger.error(f"Error fetching stores: {e}")
        return []


# ============================================================
# Date Helpers
# ============================================================
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
    """Convert datetime to GoodBooks /Date(timestamp)/ format."""
    timestamp_ms = calendar.timegm(dt.timetuple()) * 1000
    return f"/Date({timestamp_ms})/"


# ============================================================
# Apply Purchase Order
# ============================================================
def create_purchase_order(slots: Dict[str, Any], login: Dict[str, Any]) -> Dict[str, Any]:
    """Submit purchase order with dynamically built payload."""
    logger.info("Creating purchase order...")
    
    try:
        # --- Parse Dates ---
        po_date = _parse_date(slots.get("MMHeadDate")) or datetime.now()
        ref_date = _parse_date(slots.get("MMHeadReferenceDate")) or po_date
        
        # --- Extract Dynamic Data from Slots ---
        party_id = str(slots.get("PartyId", ""))
        party_code = slots.get("PartyCode", "")
        party_name = slots.get("PartyName", "")
        party_branch_id = slots.get("PartyBranchId", "-1")
        
        item_id = str(slots.get("ItemId", ""))
        item_code = slots.get("ItemCode", "")
        item_name = slots.get("ItemName", "")
        
        store_id = str(slots.get("StoreId", "-1900000000"))
        from_store_id = str(slots.get("FromStoreId", store_id))
        
        quantity = float(slots.get("Quantity", 0))
        rate = float(slots.get("Rate", 0))
        
        # --- Calculate Financial Fields ---
        item_basic_value = round(quantity * rate, 2)
        item_net_value = item_basic_value
        item_gross_value = item_basic_value
        item_cost_value = item_basic_value
        item_realisation_value = item_basic_value
        
        total_quantity = quantity
        total_net_value = item_basic_value
        total_item_basic_value = item_basic_value
        head_value = item_basic_value
        head_realisation_value = item_basic_value
        
        # --- Fetch BizTransactionTypeId Dynamically ---
        biz_type_id = get_biz_transaction_type_id(PURCHASE_TRANSACTION_CLASS_ID, login)
        
        # --- Build MMDetailArray Entry ---
        mm_detail = {
            "StoreId": store_id,
            "MMDetailSlNo": 1,
            "Item.Id": item_id,
            "ItemId": item_id,
            "ItemTolleranceType": "0",
            "Tolerancevalue": "0",
            "MMDetailDetailChargesId": -1,
            "ItemLeadTime": "0",
            "ItemCode": item_code,
            "SCNEW": "1",
            "MMDetailItemNetWeight": "0",
            "MMDetailItemTareWeight": "0",
            "MMDetailItemPostedCost": str(rate),
            "ItemPostedCost": str(rate),
            "ItemIsStockPosting": "0",
            "ItemWeight": "1",
            "BarCodeId": "-1",
            "AllocationId": "-1",
            "GeneratedAllocationId": "-1",
            "ItemIsExpirytrackingRequired": "1",
            "SchemeFreeId": "-1",
            "FreeForDocumentDetailId": "-1",
            "ItemAliasId": "-1",
            "MMDetailPlanPendingSOQuantity": "0",
            "MMDetailPlanSOH": "0",
            "MMDetailPlanScheduledReceipt": "0",
            "MMDetailPlanCalculatedQty": "0",
            "MMDetailPlanAddorLess": "0",
            "TMPPackingId": "-1",
            "L1Id": str(slots.get("L1Id", "2")),
            "L2Id": str(slots.get("L2Id", "5")),
            "L3Id": str(slots.get("L3Id", "8")),
            "L4Id": str(slots.get("L4Id", "10")),
            "L5Id": str(slots.get("L5Id", "-1")),
            "ItemName": item_name,
            "materialacid": str(slots.get("materialacid", "-1500000000")),
            "AccountId": str(slots.get("AccountId", "-1899999997")),
            "AccountName": slots.get("AccountName", "Purchase Account"),
            "AccountGroupId": str(slots.get("AccountGroupId", "-1900000000")),
            "AccountBillAllocationType": "0",
            "PackId": "-1",
            "Packyes": 1,
            "PackType": "0",
            "ItemPackSetId": "-1",
            "MMDetailBalanceQuantity": "0",
            "IsPrepaid": "0",
            "NoOfPeriods": "0",
            "DeferalPlanId": "-1",
            "DeferalPlanArray": "",
            "CombineIndentId": "-1",
            "SKUId": "-1",
            "SKUName": "NONE",
            "SKUNetWeight": "0",
            "TransactionUOMId": str(slots.get("TransactionUOMId", "-1900000000")),
            "MMDetailQuantityConversion": 1,
            "TransactionUOMName": slots.get("TransactionUOMName", "Nos"),
            "TransactionUOMNoOfDecimals": "0",
            "MMDetailQuantityConversionType": "0",
            "MMDetailItemPostedQuantity": str(quantity),
            "currentstock": "0",
            "StockPositionTotalQuantity": "0",
            "StockPositionReservedQuantity": "0",
            "StockPositionReservedFor": "0",
            "StockPositionReservedOthers": "0",
            "StockPositionOpenQuantity": "0",
            "StockPositionAvailableQuantity": "0",
            "MMDetailTransactionActualQuantity": str(quantity),
            "LotBtHidden": "1",
            "LotUniqueBtHidden": "1",
            "ItemSamplesPer": "0",
            "MMDetailNumberOfSamples": "0",
            "DocQuantity": str(quantity),
            "MMDetailRateFC": str(rate),
            "MMDetailQualityStatus": "0",
            "MMQualityVisited": "0",
            "TempOldRate": "0",
            "TempIsSlab": "1",
            "TransactionCurrencyId": str(slots.get("TransactionCurrencyId", "-1800000000")),
            "TransactionCurrencyCode": slots.get("TransactionCurrencyCode", "INR"),
            "MMDetailTransactionCurrencyConversion": "1",
            "MMDetailItemBasicValue": f"{item_basic_value:.2f}",
            "MMDetailItemPlus": "0",
            "AutoCalc": "1",
            "BCPlus": "0",
            "BCMinus": "0",
            "RVPlus": "0",
            "RVMinus": "0",
            "MMDetailItemCostValue": f"{item_cost_value:.2f}",
            "MMDetailItemRealisationValue": f"{item_realisation_value:.2f}",
            "chargetag": "0",
            "MMDetailItemMinus": "0",
            "MMDetailItemGrossValue": f"{item_gross_value:.2f}",
            "MMDetailItemMotherPlus": "0",
            "MMDetailItemMotherMinus": "0",
            "OBCPlus": "0",
            "OBCMinus": "0",
            "ORVPlus": "0",
            "ORVMinus": "0",
            "MMDetailItemNetValue": f"{item_net_value:.2f}",
            "MMDetailItemNetCost": str(rate),
            "L1Name": slots.get("L1Name", "MANDI"),
            "L2Name": slots.get("L2Name", ""),
            "L3Name": slots.get("L3Name", "IGMC_CC"),
            "L4Name": slots.get("L4Name", "AYUSHMAN "),
            "L5Name": slots.get("L5Name", ""),
            "IndentId": "-1",
            "IndentDetailId": "-1",
            "WorkCenterId": "-1",
            "OldQuantity": str(quantity),
            "AllotedBizTransactionTypeId": -1,
            "AllotedBizTransactionClassId": -1,
            "AllotedHeaderTypeId": -1,
            "AllotedHeaderObjectId": -1,
            "AllotedObjectTypeId": -1,
            "AllotedObjectId": -1,
            "AllotedForProcessOrDespatch": 0,
            "LotRequired": "1",
            "SelectedLotTypeId": -1,
            "MMDetailProductDescription": item_name,
            "TransactionRateUOMId": str(slots.get("TransactionRateUOMId", "-1900000000")),
            "MMDetailActualQuantity": quantity,
            "MMDetailGoodQuantity": str(quantity),
            "MMDetailTransactionQuantity": str(quantity),
            "MMDetailRateConversion": "1",
            "MMDetailTransactionRate": f"{rate:.2f}",
            "MMDetailRate": f"{rate:.2f}"
        }
        
        # --- Build Main Payload ---
        payload = {
            "MMHeadId": 0,
            "MMHeadNumber": "",
            "MMHeadDate": _gb_date(po_date),
            "MMHeadReferenceNumber": slots.get("ReferenceNumber", ""),
            "MMHeadReferenceDate": _gb_date(ref_date),
            "GuId": str(uuid.uuid4()),
            "OUId": login.get("WorkOUId", -1500000000),
            "MMHeadPeriodId": login.get("WorkPeriodId", 2),
            "LoadPackingId": -1,
            "PartyId": party_id,
            "PartyCode": party_code,
            "PartyName": party_name,
            "PartyBranchId": party_branch_id,
            "MMHeadPartyReferenceNumber": slots.get("PartyReferenceNumber", "1"),
            "MMHeadPartyReferenceDate": _gb_date(ref_date),
            "StoreId": store_id,
            "FromStoreId": from_store_id,
            "ToStoreId": "-1",
            "ToStoreName": "",
            "BizTransactionTypeId": biz_type_id,
            "MMHeadNoOfOrginalCopy": 0,
            "MMHeadNoOfCopyTaken": 0,
            "MMHeadRemarks": slots.get("Remarks", ""),
            "MMHeadTotalQuantity": str(total_quantity),
            "MMHeadTotalNetValue": str(total_net_value),
            "MMHeadTotalItemBasicValue": f"{total_item_basic_value:.2f}",
            "MMHeadTotalItemAmountPlus": "0",
            "MMHeadTotalItemAmountMinus": "0",
            "MMHeadTotalOtherAmountPlus": "0",
            "MMHeadTotalOtherAmountMinus": "0",
            "MMHeadCostValue": str(rate),
            "MMHeadTotalItemGrossValue": str(total_net_value),
            "MMHeadValue": f"{head_value:.2f}",
            "MMHeadRealisationValue": f"{head_realisation_value:.2f}",
            "PaymentTermId": str(slots.get("PaymentTermId", "-1899999999")),
            "TaxTransactionTypeId": str(slots.get("TaxTransactionTypeId", "-1899999994")),
            "DutyTransactionTypeId": str(slots.get("DutyTransactionTypeId", "-1899999994")),
            "RouteId": "-1",
            "MMHeadDocumentChargesId": -1,
            "InchargeId": str(slots.get("InchargeId", login.get("UserId", "-1399999756"))),
            "DepartmentId": str(slots.get("DepartmentId", "-1399999769")),
            "AllocationId": -1,
            "LotId": "-1",
            "CurrencyId": str(slots.get("CurrencyId", "-1800000000")),
            "CurrencyCode": slots.get("CurrencyCode", "INR"),
            "MMHeadCurrencyConversion": "1",
            "MMHeadPaymentTermsRemarks": "",
            "MMHeadSeason": "",
            "MMHeadSpecialInstruction": "",
            "MMHeadStatus": 1,
            "VarianceTrackingRequired": 1,
            "ReasonId": -1,
            "MMHeadPartyMaterialNumber": "",
            "MMHeadPartyAccountedNumber": "",
            "MMHeadContainerNo": "",
            "MMHeadVersion": 1,
            "LeadId": -1,
            "BizTransactionTypeBIZTransactionTypeClassId": str(PURCHASE_TRANSACTION_CLASS_ID),
            "UpdateItemParty": 4,
            "LinewiseTracking": 1,
            "POCreatedBy": "",
            "MMHeadFormsIssuedDate": _gb_date(po_date),
            "AdvanceAccountId": -1,
            "AdvanceDocumentId": -1,
            "MMHeadAdvanceAmount": 0,
            "MMHeadTotalPacks": 0,
            "MMDetailArray": [mm_detail],
            "MMChargesArray": [],
            "PartyContactId": "-1",
            "MMHeadFormsIssuedNumber": "",
            "MMHeadFormsIssuedValue": 0,
            "BillToAddressId": int(slots.get("BillToAddressId", -1900000000)),
            "ShipToAddressId": int(slots.get("ShipToAddressId", -1900000000)),
            "MMHeadFormsReceivedStatus": 1,
            "MMHeadFormsIssuedStatus": 1,
            "TermsSetId": int(slots.get("TermsSetId", -1900000000)),
            "FreightType": 0,
            "ContactId": "-1",
            "MMHeadOpenName": "",
            "MMHeadOpenPhone": "",
            "MMHeadOpenMail": "",
            "MMHeadOpenGSTNo": ""
        }
        
        logger.info(f"Submitting Purchase Order Payload: {json.dumps(payload, indent=2)}")
        
        # Use MMHead endpoint
        url = direct_url("/mms/MMHead.svc/", login)
        
        headers = {
            "Content-Type": "application/json",
            "Login": json.dumps(login)
        }
        
        response = session.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        logger.info(f"API Response: {json.dumps(result, indent=2)}")

        # --- ROBUST ERROR CHECKING ---
        if "status" in result and isinstance(result["status"], dict):
            http_code = result["status"].get("http_code")
            if http_code and http_code != 200:
                raise Exception(f"Server returned error {http_code}. The endpoint {url} might be incorrect.")

        if "contents" in result and isinstance(result["contents"], str):
            raw_content = result["contents"]
            if "\r\n\r\n" in raw_content:
                try:
                    json_part = raw_content.split("\r\n\r\n", 1)[1]
                    inner_data = json.loads(json_part)
                    inner_status = inner_data.get("Status")
                    if inner_status and inner_status != 200:
                        raise Exception(f"API Error: {inner_data.get('Body')} (Status: {inner_status})")
                except json.JSONDecodeError:
                    pass

        if isinstance(result, dict):
            if result.get("Status") == "Failed" or result.get("Status") == 400 or result.get("Status") == 412:
                raise Exception(f"API Rejected Request: {result}")

        logger.info("Purchase order created successfully")
        return result
    
    except Exception as e:
        logger.error(f"Purchase order creation failed: {e}")
        raise


if __name__ == "__main__":
    print("Purchase Client Module Loaded")
