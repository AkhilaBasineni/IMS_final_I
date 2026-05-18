# Purchase Order Workflow Enhancement — Changelog

## Version: IMS1_modified4
## Date: 2025-04-16

---

## Overview

This release implements a complete, reliable Purchase Order workflow covering:
**Create Draft → Send Email → Receive Goods → Inventory Update**

---

## Files Modified

### 1. `app/models.py`

#### `Supplier` (Table: `suppliers`)
- **Added**: `contact_email = Column(VARCHAR(255), nullable=True)`
  - Stores the supplier's email address for automated PO dispatch.

#### `PurchaseOrder` (Table: `purchase_orders`)
- **Changed**: `supplier_id` from `VARCHAR(100)` → `INTEGER FK → suppliers.supplier_id`
  - Links POs to structured Supplier records (enables email lookup, name display).
- **Added**: `supplier_rel` relationship to `Supplier` model.

---

### 2. `app/routers/purchase_order_routes.py` (Full Rewrite)

#### New: `/api/purchase-orders/<id>/send-email` [POST]
- Retrieves supplier email from `Supplier.contact_email`
- Accepts `recipient_email` override in request body if no email saved
- Validates email format with regex before attempting to send
- Generates professional HTML + plain-text email containing:
  - PO Number, Supplier Name
  - Order Date, Expected Delivery, Warehouse
  - Item table (product name, qty, unit cost, line total)
  - Grand Total, Notes
- Sends via Flask-Mail (SMTP/Gmail configured in `config.py`)
- Updates PO status: `Draft → Sent`
- Returns `needs_email: true` if email missing (frontend prompts user)

#### Modified: `/api/purchase-orders/<id>/receive` [POST]
- **Now updates BOTH `stock_levels` AND `inventory` tables** (was only `stock_levels`)
- Inventory update logic:
  ```
  prev_inv = latest Inventory record for (product_id, site_id)
  beginning_inventory = prev_inv.ending_inventory  OR  0 if none
  replenishment       = received_quantity
  ending_inventory    = beginning_inventory + replenishment
  stockout_flag       = "No" if ending_inventory > 0 else "Yes"
  ```
- **Transactional**: wrapped in try/except with `db.session.rollback()` on failure
  — no partial updates possible
- **Duplicate prevention**: returns 400 if PO already has status `Received`

#### Modified: `/api/purchase-orders/suppliers` [GET]
- Now queries `Supplier` table (previously used `Product.supplier` string column)
- Returns: `supplier_id`, `supplier_name`, `contact_email`

#### Modified: `/api/purchase-orders/products` [GET]
- Now filters by `Product.supplier_id` (FK integer) instead of `Product.supplier` (string)
- Query param changed: `supplier_id` (integer) instead of `supplier` (string)

#### Modified: `/api/purchase-orders` [POST] — Create
- `supplier_id` now validated against `Supplier` table
- Stores integer FK, not raw string

#### Modified: `/api/purchase-orders/summary` [GET]
- Returns `sent_orders` count (new `Sent` status)

#### Modified: Cancel endpoint
- Now allows cancellation of both `Draft` and `Sent` POs (previously Draft only)

---

### 3. `app/templates/purchase_orders.html` (Full Rewrite)

#### New UI Features
- **Workflow Banner**: Visual step indicator (Create → Send Email → Confirm → Receive → Updated)
- **Sent KPI Card**: Shows count of POs in "Sent" status (purple badge)
- **Send Email Button** in both the table row actions and the view modal
- **Email Modal** with:
  - PO summary header
  - Pre-filled recipient email (from supplier record)
  - Warning if no email is saved
  - Live email preview (HTML rendering of what supplier will receive)
  - Loading spinner on send button
- **Status badges**: Draft (yellow), Sent (purple), Received (green), Cancelled (red)
- **Post-create prompt**: After creating a PO, user is asked if they want to send email immediately
- **View modal** now shows status timeline + supplier email display

#### Action availability by status:
| Status    | Send Email | Receive | Cancel |
|-----------|-----------|---------|--------|
| Draft     | ✅         | ✅       | ✅      |
| Sent      | ✅ (resend)| ✅       | ✅      |
| Received  | ❌         | ❌       | ❌      |
| Cancelled | ❌         | ❌       | ❌      |

---

## New File

### `migrate_po_workflow.py`

Standalone migration script. Run **once** to apply schema changes to existing database:

```bash
python migrate_po_workflow.py
```

Steps performed:
1. Adds `suppliers.contact_email` column (idempotent)
2. Adds `purchase_orders.supplier_int_id` INTEGER FK column
3. Populates it by matching old string `supplier_id` to `suppliers.supplier_name`
4. Renames: `supplier_id → supplier_name_legacy`, `supplier_int_id → supplier_id`

---

## Workflow: Step-by-Step

```
1. User clicks "Create PO"
   → Selects supplier (from Supplier table, shows saved email)
   → Adds products (filtered by supplier_id)
   → System calculates total
   → PO saved with status = "Draft"
   → Prompt: "Send email now?"

2. User clicks "Send Email"
   → Email modal opens with pre-filled recipient
   → If no email: user enters one (saved to Supplier record for future)
   → System builds professional HTML email
   → Email sent via Flask-Mail SMTP
   → PO status → "Sent"

3. Supplier confirms externally (outside system)
   → No auto-update; system tracks status manually

4. User clicks "Mark Received"
   → Confirm dialog
   → System loops through each PO item:
       ■ Updates stock_levels.current_quantity
       ■ Creates new Inventory record:
           beginning = last ending_inventory (or 0)
           ending    = beginning + received_qty
           stockout  = "Yes"/"No"
       ■ Creates StockMovement log (PURCHASE_IN)
   → All changes atomic (rollback on any failure)
   → PO status → "Received"
   → Cannot receive again (duplicate prevention)
```

---

## Inventory Rules (Enforced)

| Rule | Enforcement |
|------|-------------|
| Inventory does NOT update on Create | ✅ Only status=Draft set |
| Inventory does NOT update on Send   | ✅ Only status=Sent set |
| Inventory updates ONLY on Receive   | ✅ Logic in `/receive` endpoint only |
| No duplicate updates                | ✅ 400 error if already Received |
| Transactional (no partial failure)  | ✅ try/except + rollback |
