from flask import Blueprint, request, jsonify
from app.models import Product, StockLevel, Site, Category, SubCategory, Supplier, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions
from sqlalchemy import func, or_

bp = Blueprint('products', __name__, url_prefix='/api/products')


def _manager_site_ids(current_user):
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


def _generate_product_id():
    """Auto-generate next product ID in format PRD10000, PRD10001, ..."""
    last = db.session.query(Product.product_id)\
        .filter(Product.product_id.like('PRD%'))\
        .order_by(Product.product_id.desc()).first()
    if last:
        try:
            num = int(last[0][3:]) + 1
        except Exception:
            num = 10000
    else:
        num = 10000
    return f'PRD{num}'


@bp.route('', methods=['GET'])
@token_required
@handle_exceptions
def get_products(current_user):
    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    search   = request.args.get('search', '').strip()
    category = request.args.get('category', '')
    status   = request.args.get('status', 'Active')

    site_ids = _manager_site_ids(current_user)

    if site_ids is not None:
        product_ids_in_state = db.session.query(StockLevel.product_id)\
            .filter(StockLevel.site_id.in_(site_ids)).distinct().subquery()
        base_q = Product.query.filter(Product.product_id.in_(product_ids_in_state))
        query  = base_q.filter(Product.status == status) if status else base_q
    else:
        query = Product.query.filter(Product.status == status) if status else Product.query

    if search:
        query = query.filter(or_(
            Product.product_name.ilike(f'%{search}%'),
            Product.product_id.ilike(f'%{search}%'),
            Product.category.ilike(f'%{search}%'),
            Product.subcategory.ilike(f'%{search}%'),
            Product.supplier.ilike(f'%{search}%')
        ))
    if category:
        query = query.filter(Product.category == category)

    pagination = query.order_by(Product.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    results = []
    for p in pagination.items:
        stock_query = db.session.query(func.sum(StockLevel.current_quantity))\
            .filter(StockLevel.product_id == p.product_id)
        if site_ids is not None:
            stock_query = stock_query.filter(StockLevel.site_id.in_(site_ids))
        stock_sum = stock_query.scalar() or 0

        results.append({
            'product_id':   p.product_id,
            'product_name': p.product_name,
            'category':     p.category,
            'subcategory':  p.subcategory,
            'unit_cost':    float(p.unit_cost),
            'unit_price':   float(p.unit_price),
            'supplier':     p.supplier,
            'supplier_id':  p.supplier_id,
            'shelf_life':   p.shelf_life,
            'total_stock':  int(stock_sum),
            'status':       p.status
        })

    return jsonify({
        'success': True,
        'data': {
            'items':        results,
            'total':        pagination.total,
            'pages':        pagination.pages,
            'current_page': page
        }
    })


@bp.route('/<product_id>', methods=['GET'])
@token_required
@handle_exceptions
def get_product(current_user, product_id):
    p = Product.query.get(product_id)
    if not p:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        exists = StockLevel.query.filter(
            StockLevel.product_id == product_id,
            StockLevel.site_id.in_(site_ids)
        ).first()
        if not exists:
            return jsonify({'success': False, 'message': 'Access denied'}), 403

    return jsonify({'success': True, 'data': {
        'product_id':   p.product_id,
        'product_name': p.product_name,
        'category':     p.category,
        'subcategory':  p.subcategory,
        'unit_cost':    float(p.unit_cost),
        'unit_price':   float(p.unit_price),
        'supplier':     p.supplier,
        'supplier_id':  p.supplier_id,
        'shelf_life':   p.shelf_life,
        'status':       p.status
    }})


@bp.route('', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def create_product(current_user):
    data = request.json
    if not data.get('product_name'):
        return jsonify({'success': False, 'message': 'Product Name is required'}), 400

    product_id = _generate_product_id()

    supplier_name = data.get('supplier')
    supplier_id   = data.get('supplier_id')
    if supplier_id:
        sup = Supplier.query.get(supplier_id)
        if sup:
            supplier_name = sup.supplier_name

    new_prod = Product(
        product_id   = product_id,
        product_name = data['product_name'],
        category     = data.get('category'),
        subcategory  = data.get('subcategory'),
        unit_cost    = data.get('unit_cost', 0),
        unit_price   = data.get('unit_price', 0),
        supplier     = supplier_name,
        supplier_id  = supplier_id if supplier_id else None,
        shelf_life   = data.get('shelf_life'),
        status       = 'Active'
    )
    db.session.add(new_prod)
    db.session.flush()

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        for site_id in site_ids:
            existing = StockLevel.query.filter_by(
                product_id=new_prod.product_id, site_id=site_id
            ).first()
            if not existing:
                db.session.add(StockLevel(
                    product_id=new_prod.product_id,
                    site_id=site_id,
                    current_quantity=0
                ))

    db.session.commit()
    log_audit(current_user, 'CREATE', 'Product', new_prod.product_id)
    return jsonify({'success': True, 'message': 'Product created successfully',
                    'product_id': product_id}), 201


@bp.route('/<product_id>', methods=['PUT'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def update_product(current_user, product_id):
    p = Product.query.get(product_id)
    if not p:
        return jsonify({'success': False, 'message': 'Product not found'}), 404

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        exists = StockLevel.query.filter(
            StockLevel.product_id == product_id,
            StockLevel.site_id.in_(site_ids)
        ).first()
        if not exists:
            return jsonify({'success': False, 'message': 'Access denied'}), 403

    data = request.json

    supplier_id   = data.get('supplier_id', p.supplier_id)
    supplier_name = data.get('supplier', p.supplier)
    if supplier_id:
        sup = Supplier.query.get(supplier_id)
        if sup:
            supplier_name = sup.supplier_name

    p.product_name = data.get('product_name', p.product_name)
    p.category     = data.get('category',     p.category)
    p.subcategory  = data.get('subcategory',  p.subcategory)
    p.unit_cost    = data.get('unit_cost',    p.unit_cost)
    p.unit_price   = data.get('unit_price',   p.unit_price)
    p.supplier     = supplier_name
    p.supplier_id  = supplier_id
    p.shelf_life   = data.get('shelf_life',   p.shelf_life)
    db.session.commit()
    log_audit(current_user, 'UPDATE', 'Product', product_id)
    return jsonify({'success': True, 'message': 'Product updated successfully'})


@bp.route('/<product_id>', methods=['DELETE'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def delete_product(current_user, product_id):
    p = Product.query.get(product_id)
    if not p:
        return jsonify({'success': False, 'message': 'Product not found'}), 404

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        exists = StockLevel.query.filter(
            StockLevel.product_id == product_id,
            StockLevel.site_id.in_(site_ids)
        ).first()
        if not exists:
            return jsonify({'success': False, 'message': 'Access denied'}), 403

    p.status = 'Inactive'
    db.session.commit()
    log_audit(current_user, 'DEACTIVATE', 'Product', product_id)
    return jsonify({'success': True, 'message': 'Product deactivated successfully'})


# ─── CATEGORY ROUTES ──────────────────────────────────────────────────────────

@bp.route('/categories/all', methods=['GET'])
@token_required
@handle_exceptions
def get_categories(current_user):
    cats = Category.query.filter_by(status='Active').order_by(Category.category_name).all()
    return jsonify({'success': True, 'data': [
        {'id': c.id, 'category_name': c.category_name,
         'subcategories': [
             {'id': s.id, 'subcategory_name': s.subcategory_name}
             for s in c.subcategories if s.status == 'Active'
         ]}
        for c in cats
    ]})


@bp.route('/categories/add', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def add_category(current_user):
    data = request.json
    cat_name    = data.get('category_name', '').strip()
    subcat_name = data.get('subcategory_name', '').strip()

    if not cat_name:
        return jsonify({'success': False, 'message': 'Category name is required'}), 400

    cat = Category.query.filter_by(category_name=cat_name).first()
    if not cat:
        cat = Category(category_name=cat_name, status='Active')
        db.session.add(cat)
        db.session.flush()

    if subcat_name:
        existing_sub = SubCategory.query.filter_by(
            subcategory_name=subcat_name, category_id=cat.id
        ).first()
        if not existing_sub:
            db.session.add(SubCategory(
                subcategory_name=subcat_name,
                category_id=cat.id,
                status='Active'
            ))

    db.session.commit()
    log_audit(current_user, 'CREATE', 'Category', cat.id, {'category_name': cat.category_name})
    return jsonify({'success': True, 'message': 'Category saved',
                    'category_id': cat.id, 'category_name': cat.category_name}), 201


# ─── SUPPLIER ROUTES ──────────────────────────────────────────────────────────

@bp.route('/suppliers/all', methods=['GET'])
@token_required
@handle_exceptions
def get_suppliers(current_user):
    suppliers = Supplier.query.filter_by(status='Active').order_by(Supplier.supplier_name).all()
    return jsonify({'success': True, 'data': [
        {'supplier_id': s.supplier_id, 'supplier_name': s.supplier_name}
        for s in suppliers
    ]})


@bp.route('/suppliers/add', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def add_supplier(current_user):
    data  = request.json
    name  = data.get('supplier_name', '').strip()
    email = data.get('contact_email', '').strip() or None
    if not name:
        return jsonify({'success': False, 'message': 'Supplier name is required'}), 400
    if Supplier.query.filter_by(supplier_name=name).first():
        return jsonify({'success': False, 'message': 'Supplier already exists'}), 400
    sup = Supplier(supplier_name=name, contact_email=email, status='Active')
    db.session.add(sup)
    db.session.commit()
    log_audit(current_user, 'CREATE', 'Supplier', sup.supplier_id, {'supplier_name': sup.supplier_name})
    return jsonify({'success': True, 'message': 'Supplier added',
                    'supplier_id':   sup.supplier_id,
                    'supplier_name': sup.supplier_name,
                    'contact_email': sup.contact_email or ''}), 201
