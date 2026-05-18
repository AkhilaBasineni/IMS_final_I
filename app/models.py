from app.database import db
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import JSONB


def _utcnow():
    """Return current UTC time as a timezone-aware datetime.
    Replaces the deprecated datetime.utcnow() which returns a naive datetime
    and causes SQLAlchemy 2.x / PostgreSQL timezone-aware column mismatches.
    """
    return datetime.now(timezone.utc)

# 1. roles table
class Role(db.Model):
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    role_name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    users = db.relationship('User', backref='role', lazy=True)

# 2. states table
class State(db.Model):
    __tablename__ = 'states'
    state_id = db.Column(db.Integer, primary_key=True)
    state_name = db.Column(db.String(100), unique=True, nullable=False)
    sites = db.relationship('Site', backref='state', lazy=True)

# 3. managers table
class Manager(db.Model):
    __tablename__ = 'managers'
    manager_id = db.Column(db.Integer, primary_key=True)
    manager_name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  
    sites = db.relationship('Site', backref='manager', lazy=True)  

# 4. users table
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False) 
    state_id = db.Column(db.String(50), nullable=True)  
    is_active = db.Column(db.Boolean, default=True)
    photo_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    manager = db.relationship('Manager', backref='user', lazy=True, uselist=False)  


# 5. products table
class Product(db.Model):
    __tablename__ = 'products'
    product_id = db.Column(db.String(50), primary_key=True)
    product_name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100))
    subcategory = db.Column(db.String(100))
    unit_cost = db.Column(db.Numeric(10, 2), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    supplier = db.Column(db.String(100))
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.supplier_id'), nullable=True)
    shelf_life = db.Column(db.Integer)
    reorder_point = db.Column(db.Integer, default=50)
    status = db.Column(db.String(20), default='Active')
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    supplier_rel = db.relationship('Supplier', backref='products', foreign_keys=[supplier_id])

# 6. categories table
class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    category_name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default='Active')
    subcategories = db.relationship('SubCategory', backref='category', lazy=True, cascade='all, delete-orphan')

# 6b. subcategories table
class SubCategory(db.Model):
    __tablename__ = 'subcategories'
    id = db.Column(db.Integer, primary_key=True)
    subcategory_name = db.Column(db.String(100), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    status = db.Column(db.String(20), default='Active')
    __table_args__ = (db.UniqueConstraint('subcategory_name', 'category_id', name='_subcat_cat_uc'),)

# 6c. suppliers table
class Supplier(db.Model):
    __tablename__ = 'suppliers'
    supplier_id = db.Column(db.Integer, primary_key=True)
    supplier_name = db.Column(db.String(150), unique=True, nullable=False)
    contact_email = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='Active')
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

# 7. sites table

class Site(db.Model):
    __tablename__ = 'sites'
    site_id = db.Column(db.String(50), primary_key=True)
    site_name = db.Column(db.String(150), nullable=False)
    site_format = db.Column(db.String(50))
    region = db.Column(db.String(50))
    city = db.Column(db.String(100))
    state_id = db.Column(db.Integer, db.ForeignKey('states.state_id'))
    store_size = db.Column(db.Integer)
    open_date = db.Column(db.Date)
    status = db.Column(db.String(20))
    manager_id = db.Column(db.Integer, db.ForeignKey('managers.manager_id'), nullable=True)  
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
# 8. customers table
class Customer(db.Model):
    __tablename__ = 'customers'
    customer_id = db.Column(db.String(50), primary_key=True)
    name = db.Column(db.String(150), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    age = db.Column(db.Integer)
    gender = db.Column(db.String(20))
    income_bracket = db.Column(db.String(50))
    purchase_frequency = db.Column(db.Integer)
    average_spend = db.Column(db.Numeric(10, 2))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

# 9. inventory table
class Inventory(db.Model):
    __tablename__ = 'inventory'
    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    beginning_inventory = db.Column(db.Integer)
    ending_inventory = db.Column(db.Integer)
    replenishment = db.Column(db.Integer)
    stockout_flag = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    product_rel = db.relationship('Product', backref='inventory_records', lazy=True, foreign_keys=[product_id])
    site_rel    = db.relationship('Site',    backref='inventory_records', lazy=True, foreign_keys=[site_id])

# 10. logistics table
class Logistics(db.Model):
    __tablename__ = 'logistics'
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.String(50), unique=True, nullable=False)
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    shipment_date = db.Column(db.Date)
    quantity = db.Column(db.Integer)
    delivery_status = db.Column(db.String(50))
    transportation_type = db.Column(db.String(50))

# 11. sales table
class Sale(db.Model):
    __tablename__ = 'sales'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date)
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    units_sold = db.Column(db.Integer)
    revenue = db.Column(db.Numeric(10, 2))
    discounts = db.Column(db.Numeric(10, 2))
    returns = db.Column(db.Integer)
    customer_id = db.Column(db.String(50), db.ForeignKey('customers.customer_id'), nullable=True)

# 12. promotions table
class Promotion(db.Model):
    __tablename__ = 'promotions'
    id = db.Column(db.Integer, primary_key=True)
    promotion_id = db.Column(db.String(50), unique=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    discount_type = db.Column(db.String(20))
    discount_amount = db.Column(db.Numeric(10, 2))

# 13. seasonal_plans table
class SeasonalPlan(db.Model):
    __tablename__ = 'seasonal_plans'
    id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.String(20))
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    product_category = db.Column(db.String(100))
    forecasted_sales = db.Column(db.Numeric(12, 2))
    actual_sales = db.Column(db.Numeric(12, 2), default=0)
    seasonal_adjustments = db.Column(db.Numeric(5, 3), default=0)
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    __table_args__ = (db.UniqueConstraint('month', 'site_id', 'product_category',
                                          name='_seasonal_plan_uc'),)

# 14. stock_levels table
class StockLevel(db.Model):
    __tablename__ = 'stock_levels'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'), nullable=False)
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'), nullable=False)
    current_quantity = db.Column(db.Integer, default=0)
    last_updated = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    product = db.relationship('Product', backref='stock_levels')
    site = db.relationship('Site', backref='stock_levels')
    __table_args__ = (db.UniqueConstraint('product_id', 'site_id', name='_prod_site_uc'),)

# 15. stock_movements table
class StockMovement(db.Model):
    __tablename__ = 'stock_movements'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    quantity = db.Column(db.Integer)
    movement_type = db.Column(db.String(50))
    reference_id = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

# 16. purchase_orders table
class PurchaseOrder(db.Model):
    __tablename__ = 'purchase_orders'
    id = db.Column(db.Integer, primary_key=True)
    po_number = db.Column(db.String(50), unique=True, nullable=False)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.supplier_id'), nullable=True)
    warehouse_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    order_date = db.Column(db.Date)
    expected_delivery = db.Column(db.Date)
    status = db.Column(db.String(20), default='Draft')
    total_amount = db.Column(db.Numeric(12, 2))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    items = db.relationship('PurchaseOrderItem', backref='order', cascade="all, delete-orphan")
    warehouse = db.relationship('Site', backref='purchase_orders')
    supplier_rel = db.relationship('Supplier', backref='purchase_orders', foreign_keys=[supplier_id])

# 17. purchase_order_items table
class PurchaseOrderItem(db.Model):
    __tablename__ = 'purchase_order_items'
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey('purchase_orders.id'))
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    quantity = db.Column(db.Integer)
    received_quantity = db.Column(db.Integer, default=0)
    unit_price = db.Column(db.Numeric(10, 2))
    line_total = db.Column(db.Numeric(12, 2))
    product = db.relationship('Product')

# 18. sales_orders table
class SalesOrder(db.Model):
    __tablename__ = 'sales_orders'
    id = db.Column(db.Integer, primary_key=True)
    so_number = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.String(50), db.ForeignKey('customers.customer_id'), nullable=True)
    warehouse_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    order_date = db.Column(db.Date)
    status = db.Column(db.String(20), default='Draft')
    total_amount = db.Column(db.Numeric(12, 2))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    confirmed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    shipping_address = db.Column(db.Text)
    tracking_number = db.Column(db.String(100))
    transport = db.Column(db.String(20))
    notes = db.Column(db.Text)
    discount = db.Column(db.Numeric(10, 2), default=0)
    email_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    items = db.relationship('SalesOrderItem', backref='order', cascade="all, delete-orphan")
    warehouse = db.relationship('Site', backref='sales_orders')
    customer = db.relationship('Customer', backref='sales_orders', foreign_keys=[customer_id])

# 19. sales_order_items table
class SalesOrderItem(db.Model):
    __tablename__ = 'sales_order_items'
    id = db.Column(db.Integer, primary_key=True)
    so_id = db.Column(db.Integer, db.ForeignKey('sales_orders.id'))
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    quantity = db.Column(db.Integer)
    shipped_quantity = db.Column(db.Integer, default=0)
    unit_price = db.Column(db.Numeric(10, 2))
    line_total = db.Column(db.Numeric(12, 2))
    product = db.relationship('Product')

# 20. stock_transfers table
class StockTransfer(db.Model):
    __tablename__ = 'stock_transfers'
    id = db.Column(db.Integer, primary_key=True)
    transfer_number = db.Column(db.String(50), unique=True)
    from_warehouse = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    to_warehouse = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    quantity = db.Column(db.Integer)
    status = db.Column(db.String(20), default='Pending')
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    transfer_date = db.Column(db.Date)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

# 21. audit_logs table
class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    username = db.Column(db.String(100))
    role = db.Column(db.String(50))
    action = db.Column(db.String(50))
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.String(100))
    details = db.Column(JSONB)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)

# 22. stock_adjustment_requests table
class StockAdjustmentRequest(db.Model):
    __tablename__ = 'stock_adjustment_requests'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    site_id = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    requested_quantity = db.Column(db.Integer)
    adjustment_type = db.Column(db.String(20))
    reason = db.Column(db.String(255))
    notes = db.Column(db.Text)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    status = db.Column(db.String(20), default='Pending')
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
# 23. contact_messages table
class ContactMessage(db.Model):
    __tablename__ = 'contact_messages'
    id           = db.Column(db.Integer, primary_key=True)
    first_name   = db.Column(db.String(100), nullable=False)
    last_name    = db.Column(db.String(100), nullable=False)
    email        = db.Column(db.String(255), nullable=False)
    message      = db.Column(db.Text, nullable=False)
    status       = db.Column(db.String(20), default='unread')  # unread | read | replied
    admin_reply  = db.Column(db.Text, nullable=True)
    replied_at   = db.Column(db.DateTime, nullable=True)
    created_at   = db.Column(db.DateTime, default=_utcnow)

# 24. sales_order_returns table
class SalesOrderReturn(db.Model):
    __tablename__ = 'sales_order_returns'
    id             = db.Column(db.Integer, primary_key=True)
    return_number  = db.Column(db.String(80), unique=True, nullable=False)
    so_id          = db.Column(db.Integer, db.ForeignKey('sales_orders.id'), nullable=False)
    warehouse_id   = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    status         = db.Column(db.String(20), nullable=False, default='Pending')
    total_refund   = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    notes          = db.Column(db.Text)
    created_by     = db.Column(db.Integer, db.ForeignKey('users.id'))
    processed_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    processed_at   = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_utcnow)
    items          = db.relationship('SalesOrderReturnItem', backref='return_record',
                                     cascade='all, delete-orphan')

# 25. sales_order_return_items table
class SalesOrderReturnItem(db.Model):
    __tablename__ = 'sales_order_return_items'
    id         = db.Column(db.Integer, primary_key=True)
    return_id  = db.Column(db.Integer, db.ForeignKey('sales_order_returns.id'), nullable=False)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    return_qty = db.Column(db.Integer, nullable=False)
    condition  = db.Column(db.String(20), nullable=False)  # Good | Damaged
    damage_by  = db.Column(db.String(20), nullable=True)   # our_damage | customer_damage (SO returns only)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    reason     = db.Column(db.String(255))

# 26. purchase_order_returns table
class PurchaseOrderReturn(db.Model):
    __tablename__ = 'purchase_order_returns'
    id             = db.Column(db.Integer, primary_key=True)
    return_number  = db.Column(db.String(80), unique=True, nullable=False)
    po_id          = db.Column(db.Integer, db.ForeignKey('purchase_orders.id'), nullable=False)
    warehouse_id   = db.Column(db.String(50), db.ForeignKey('sites.site_id'))
    status         = db.Column(db.String(20), nullable=False, default='Pending')
    total_credit   = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    notes          = db.Column(db.Text)
    created_by     = db.Column(db.Integer, db.ForeignKey('users.id'))
    processed_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    processed_at   = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=_utcnow)
    items          = db.relationship('PurchaseOrderReturnItem', backref='return_record',
                                     cascade='all, delete-orphan')

# 27. purchase_order_return_items table
class PurchaseOrderReturnItem(db.Model):
    __tablename__ = 'purchase_order_return_items'
    id         = db.Column(db.Integer, primary_key=True)
    return_id  = db.Column(db.Integer, db.ForeignKey('purchase_order_returns.id'), nullable=False)
    product_id = db.Column(db.String(50), db.ForeignKey('products.product_id'))
    return_qty = db.Column(db.Integer, nullable=False)
    condition  = db.Column(db.String(20), nullable=False)  # Good | Damaged
    damage_by  = db.Column(db.String(20), nullable=True)   # supplier_damage | our_damage (PO returns only)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    reason     = db.Column(db.String(255))
