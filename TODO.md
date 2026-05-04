# Purchase Order Implementation TODO

## Step 1: Create `backend/purchase_bot/` module
- [x] `__init__.py`
- [x] `purchase_prompt.py` — LLM system prompt for PO intent & slots
- [x] `purchase_agent.py` — Intent resolver, regex fallback, slot normalization
- [x] `purchase_client.py` — API client: get_parties, get_items, create_purchase_order

## Step 2: Update shared config files
- [x] `backend/biztransactionid/__init__.py` — Add `PURCHASE_TRANSACTION_CLASS_ID`
- [x] `backend/criteria.json` — Add `PARTY_CRITERIA`, `ITEM_CRITERIA`, `STORE_CRITERIA`

## Step 3: Update `backend/main.py`
- [x] Add imports for purchase_bot
- [x] Add `PURCHASE_STATE` and TTL cleanup
- [x] Extend `resolve_intent()` with purchase signals
- [x] Add active purchase flow handler
- [x] Add new intent trigger for "purchase"

## Step 4: Test & Validate
- [ ] Run server
- [ ] Test PO creation flow via API
