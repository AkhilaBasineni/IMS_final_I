-- Migration: Add damage_by column to return item tables
-- Run this once on your database before deploying the updated code.

-- For Purchase Order Return Items:
--   'supplier_damage' = supplier's fault → deduct stock + credit claimed from supplier
--   'our_damage'      = our fault        → deduct stock + no credit (we absorb the loss)
ALTER TABLE purchase_order_return_items
  ADD COLUMN IF NOT EXISTS damage_by VARCHAR(20) DEFAULT NULL;

-- For Sales Order Return Items:
--   'our_damage'       = our fault        → restock + full refund to customer
--   'customer_damage'  = customer's fault → not restocked, no refund
ALTER TABLE sales_order_return_items
  ADD COLUMN IF NOT EXISTS damage_by VARCHAR(20) DEFAULT NULL;
