import httpx
import json
import base64
from typing import Dict, Any, Optional
from datetime import datetime
from config import settings

# Create a single client instance for reuse
client = httpx.Client(timeout=settings.GB_API_TIMEOUT or 60.0)

# ============================================================
# HEADER BUILDER
# ============================================================
def _build_login_header(login_dto: Dict[str, Any]) -> Dict[str, str]:
    """Generate Login header with base64 encoded JSON"""
    try:
        encoded = base64.b64encode(
            json.dumps(login_dto, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("utf-8")
        return {
            "Login": encoded,
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "http://183.82.250.223:92/apps/main.html",
            "User-Agent": "Mozilla/5.0 GoodbooksBot"
        }
    except Exception as e:
        print(f"⚠️  Error building login header: {e}")
        raise

# ============================================================
# TYPE CONVERTERS
# ============================================================
def _map_conversion_type(value) -> int:
    """
    Convert text ConversionType to numeric code
    Fixed=0, Multiply=1, Divide=2
    """
    if value is None:
        return 0
    
    # If already an integer, return it
    if isinstance(value, int):
        return value
    
    # Convert string to lowercase for comparison
    text = str(value).lower().strip()
    
    if text == "fixed":
        return 0
    elif text in ("multiply", "multiplied"):
        return 1
    elif text in ("divide", "divided"):
        return 2
    
    # Default to Fixed if unknown
    return 0

def _safe_conversion_factor(value) -> str:
    """
    Convert ConversionFactor to string format for API
    Ensures valid numeric string
    """
    if value is None or value == "":
        return "0"
    
    try:
        # Try to convert to number first to validate
        num = float(str(value).strip())
        
        # If it's a whole number, return as int string
        if num == int(num):
            return str(int(num))
        
        # Otherwise return as float string
        return str(num)
        
    except (ValueError, TypeError):
        print(f"Invalid conversion factor: {value}, using 0")
        return "0"

# ============================================================
# TIMESTAMP GENERATOR
# ============================================================
def get_gb_timestamp():
    # Convert to UI compatible format: remove trailing /
    ts = int(datetime.now().timestamp() * 1000)
    return f"/Date({ts})/"

# ============================================================
# SELECT/SEARCH PACKS
# ============================================================
def select_packs(payload: Dict[str, Any],
                 login_dto: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Select/Search packs from GoodBooks
    
    Args:
        payload: Search parameters (SearchText, PageIndex, PageSize)
        login_dto: Login credentials
        
    Returns:
        Response with Items list
    """
    
    # Mock mode for testing
    if settings.GB_MOCK_MODE:
        mock_items = []
        search_text = payload.get("SearchText", "").lower()
        
        # Generate some mock data
        if not search_text or "mock" in search_text:
            mock_items = [
                {
                    "PackId": 1001,
                    "PackCode": "MOCK001",
                    "PackName": "Mock Pack 1",
                    "PackConversionType": 0,
                    "PackConversionFactor": 1,
                    "PackCreatedOn": get_gb_timestamp(),
                    "PackModifiedOn": get_gb_timestamp(),
                    "PackCreatedByName": "SYSTEM",
                    "PackModifiedByName": "SYSTEM"
                }
            ]
        
        return {
            "Items": mock_items,
            "TotalCount": len(mock_items),
            "Success": True,
            "Message": "Mock data returned"
        }
    
    # Real API call - Use the correct endpoint
    url = f"{settings.GB_API_BASE}/mms/Pack.svc/SelectList"
    headers = _build_login_header(login_dto or settings.GB_LOGIN_DTO)

    if settings.DEBUG:
        print(f"\n{'='*60}")
        print(f"SELECT PACKS REQUEST")
        print(f"{'='*60}")
        print(f"URL: {url}")
        print(f"Payload: {json.dumps(payload, indent=2)}")

    try:
        r = client.post(url, json=payload, headers=headers)
        
        if settings.DEBUG:
            print(f"Status Code: {r.status_code}")
            print(f"Response Preview: {r.text[:300]}...")
        
        r.raise_for_status()
        result = r.json()
        
        if settings.DEBUG:
            items_count = len(result.get('Items', []))
            print(f"✓ SUCCESS - Found {items_count} pack(s)")
            print(f"{'='*60}\n")
        
        return result
        
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        print(f"✗ Select packs failed: {error_msg}")
        raise Exception(error_msg)
        
    except Exception as e:
        error_msg = f"Failed to select packs: {str(e)}"
        print(f"✗ {error_msg}")
        raise Exception(error_msg)

# ============================================================
# SAVE PACK (CREATE/UPDATE)
# ============================================================
def save_pack(pack: Dict[str,Any], login=None):

    login = login or settings.GB_LOGIN_DTO  # MUST BE FULL LOGIN JSON FROM UI

    payload = {
        "PackCode": pack["PackCode"],
        "PackConversionFactor": str(pack["PackConversionFactor"]),
        "PackConversionType": int(pack["PackConversionType"]),
        "PackCreatedByName": login["UserCode"],              # SAME AS UI DOES
        "PackCreatedOn": get_gb_timestamp(),
        "PackId": int(pack.get("PackId",0)),
        "PackModifiedByName": login["UserCode"],
        "PackModifiedOn": get_gb_timestamp(),
        "PackName": pack["PackName"],
        "PackStatus": 1,
        "PackVersion": 1
    }

    url = "http://183.82.250.223:92/apps/proxy.php?url=http://newserver:81/gb4/mms/Pack.svc/"

    headers = {
        "Login": json.dumps(login),                    
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0)",
        "Origin": "http://183.82.250.223:92",
        "Referer": "http://183.82.250.223:92/apps/main.html"
    }

    print("\n=============== FINAL REQUEST SENT ===============")
    print(json.dumps(payload, indent=2))
    print("POST →", url)

    r = client.post(url, json=payload, headers=headers)
    print("\nSTATUS:", r.status_code)
    print("SERVER RESPONSE:", r.text[:300])

    r.raise_for_status()
    return r.json()

# ============================================================
# DELETE PACK
# ============================================================
def delete_pack(pack_id: int, login_dto=None):
    """
    Correct DELETE pack function (EXACT GoodBooks behavior)
    """

    login = login_dto or settings.GB_LOGIN_DTO

    # Correct URL → DELETE with PackId in query string
    url = (
        "http://183.82.250.223:92/apps/proxy.php?"
        f"url=http://newserver:81/gb4/mms/Pack.svc/?PackId={pack_id}"
    )

    # CORRECT headers (NO base64, plain JSON!)
    headers = {
        "Login": json.dumps(login),
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "http://183.82.250.223:92/apps/main.html",
        "Origin": "http://183.82.250.223:92",
        "User-Agent": "Mozilla/5.0"
    }

    if settings.DEBUG:
        print("\n================ DELETE PACK ================")
        print("URL:", url)
        print("HEADERS:", json.dumps(headers, indent=2))
        print(f"Deleting PackId = {pack_id}")
        print("============================================")

    try:
        # MUST BE DELETE METHOD
        response = client.delete(url, headers=headers)

        if settings.DEBUG:
            print("Status:", response.status_code)
            print("Response:", response.text[:300])

        response.raise_for_status()

        # GB returns JSON always
        return response.json()

    except Exception as e:
        print(f"Delete failed: {e}")
        raise Exception(f"Failed to delete pack: {e}")

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def get_pack_by_code(pack_code: str, 
                     login_dto: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Helper to get a single pack by code
    
    Args:
        pack_code: Pack code to search for
        login_dto: Login credentials
        
    Returns:
        Pack data if found, None otherwise
    """
    try:
        result = select_packs({
            "SearchText": pack_code,
            "PageIndex": 1,
            "PageSize": 10
        }, login_dto)
        
        items = result.get("Items", [])
        
        # Return first match if found
        if items:
            return items[0]
        
        return None
        
    except Exception as e:
        print(f"Error getting pack by code: {e}")
        return None

def get_pack_by_id(pack_id: int,
                   login_dto: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Helper to get a single pack by ID
    
    Args:
        pack_id: Pack ID to search for
        login_dto: Login credentials
        
    Returns:
        Pack data if found, None otherwise
    """
    try:
        result = select_packs({
            "SearchText": "",
            "PageIndex": 1,
            "PageSize": 100
        }, login_dto)
        
        items = result.get("Items", [])
        
        # Find pack with matching ID
        for item in items:
            if item.get("PackId") == pack_id:
                return item
        
        return None
        
    except Exception as e:
        print(f"Error getting pack by ID: {e}")
        return None

# ============================================================
# CLIENT CLEANUP
# ============================================================
def close_client():
    """Close the HTTP client when done"""
    try:
        client.close()
    except Exception as e:
        print(f"Error closing client: {e}")
    

def get_all_packs(login):
    url = f"{settings.GB_API_BASE}/mms/Pack.svc"
    headers = {"Login": json.dumps(login)}
     
    with httpx.Client(timeout=settings.GB_API_TIMEOUT) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

