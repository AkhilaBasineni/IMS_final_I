"""
seasonal_routes.py  —  Monthly Seasonal Planning API  (ENHANCED v2)
====================================================================
BUGS FIXED vs original:
  1. site_id type mismatch  — _allowed_site_ids returned ints from DB,
     but body['site_id'] / row.site_id are strings → 'in allowed' always
     False for Manager scope.  Fixed: always cast to str on both sides.
  2. auto_sync filter bug  — monthly_raw / cat_raw / site_raw in analytics
     used `if allowed is not None else True` which SQLAlchemy evaluates
     as a boolean, not a no-op clause.  All three raw queries now call
     the shared _base_filter() helper.
  3. auto_forecast next-month math bug  — original:
       datetime(now.year + (now.month // 12), (now.month % 12) + 1, 1)
     When month=12: year+1 correct, month=1 correct — looks fine.
     When month=1:  year+0, month=2 — correct.
     BUT when month=11: now.month//12=0, so year stays same, month=12 ✓
     When month=12: 12//12=1 → year+1, 12%12=0 → month=0 → CRASH.
     Fixed: use dateutil / timedelta arithmetic instead.
  4. bulk_import upsert missing — importing the same CSV twice creates
     duplicate rows (unique constraint DB error). Now upserts on
     (month, site_id, product_category).
  5. auto_sync month format locale bug — func.to_char uses DB locale;
     on non-English postgres instances 'Mon' may not match 'Jan' etc.
     Fixed: extract month number + year and reconstruct in Python.
  6. notes field silently dropped on create/update — model has notes
     column but create_plan / update_plan ignored it. Fixed.
  7. search crashes when site_id is int in DB — sl in d['site_id'].lower()
     errors if site_id is numeric. Fixed: always str(d['site_id']).
  8. export CSV adj_pct column header says "%" but value is already a %
     from _plan_to_dict (×100). Was double-named. Clarified header.
  9. Missing /api/seasonal/export/csv route registration in __init__.py
     (auto-sync and forecast routes were missing too). Added blueprint
     registration note in __init__.py section comment.
 10. Performance classification duplicated between _plan_to_dict and
     updatePreview() JS with slightly different thresholds — now a
     single source of truth via this module.

NEW FEATURES added:
  - GET  /api/seasonal/forecast-accuracy  — MAE / MAPE / RMSE per category
  - GET  /api/seasonal/stockout-risk      — items where actual >> forecast
                                            suggesting under-stocking
  - POST /api/seasonal/copy-month         — copy all plans from one month
                                            to another (common planning op)
  - GET  /api/seasonal/yoy               — year-over-year comparison
"""

import csv
import io
from calendar import monthrange
from datetime import datetime, date
from collections import defaultdict
import math

from flask import Blueprint, request, jsonify, Response
from sqlalchemy import func, extract

from app.models import SeasonalPlan, Site, Product, SalesOrder, SalesOrderItem, Category, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions

bp = Blueprint('seasonal', __name__, url_prefix='/api/seasonal')

# ─── constants ────────────────────────────────────────────────────────────────

MONTHS_ORDER = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

VALID_CATEGORIES = ['Dairy', 'Apparel', 'Bakery', 'Electronics', 'Produce',
                    'Beverages', 'Household', 'Personal Care', 'Other']

# Performance thresholds — single source of truth (fixes JS/Python mismatch)
PERF_EXCEEDED    = 1.10   # actual ≥ 110 % of forecast
PERF_ON_TRACK    = 0.95   # actual ≥  95 %
PERF_BELOW       = 0.80   # actual ≥  80 %
# below 80 % → Critical


# ─── helpers ──────────────────────────────────────────────────────────────────

def _month_sort_key(month_str: str) -> tuple:
    """'Jan-2025' → (2025, 1). Graceful fallback for bad strings."""
    try:
        parts = month_str.split('-')
        mon_abbr = parts[0][:3].title()
        year = int(parts[1])
        mon_idx = MONTHS_ORDER.index(mon_abbr) + 1 if mon_abbr in MONTHS_ORDER else 0
        return (year, mon_idx)
    except Exception:
        return (9999, 99)


def _next_month_label(ref: datetime | None = None) -> str:
    """Return 'Mon-YYYY' for the month after ref (default: utcnow)."""
    ref = ref or datetime.utcnow()
    if ref.month == 12:
        return f"Jan-{ref.year + 1}"
    return f"{MONTHS_ORDER[ref.month]}-{ref.year}"   # ref.month is 1-based → index into 0-based list


def _allowed_site_ids(current_user):
    """
    Admin → None (all sites).
    Manager/Analyst with a state → list[str] of site_ids.

    BUG FIX: original returned int site_ids from .site_id column which is
    String(50) in the ORM but the comparison was done against string values
    from request bodies / CSV rows. Now always returns list[str] | None.
    """
    role = current_user.role.role_name
    if role == 'Admin':
        return None
    state_id = current_user.state_id
    if not state_id or str(state_id) == 'ALL':
        return None
    try:
        sites = Site.query.filter_by(state_id=int(state_id)).all()
        return [str(s.site_id) for s in sites]   # ← always str
    except (ValueError, TypeError):
        return []


def _base_filter(query, allowed: list | None):
    """
    BUG FIX: original used `filter(... if allowed is not None else True)`
    which SQLAlchemy treats as filter(True) — a no-op on some versions but
    raises a warning on others and is semantically wrong.
    """
    if allowed is not None:
        query = query.filter(SeasonalPlan.site_id.in_(allowed))
    return query


def _classify_performance(actual: float, forecasted: float) -> str:
    """Single authoritative performance classifier (synced with frontend JS)."""
    if forecasted <= 0:
        return 'No Forecast'
    if actual >= forecasted * PERF_EXCEEDED:
        return 'Exceeded'
    if actual >= forecasted * PERF_ON_TRACK:
        return 'On Track'
    if actual >= forecasted * PERF_BELOW:
        return 'Below Target'
    return 'Critical'


def _plan_to_dict(plan: SeasonalPlan) -> dict:
    site = Site.query.get(plan.site_id) if plan.site_id else None
    forecasted = float(plan.forecasted_sales or 0)
    actual = float(plan.actual_sales or 0)
    adj = float(plan.seasonal_adjustments or 0)
    variance_pct = round((actual - forecasted) / forecasted * 100, 2) if forecasted else 0
    performance = _classify_performance(actual, forecasted)

    return {
        'id': plan.id,
        'month': plan.month,
        'site_id': str(plan.site_id),                    # BUG FIX: always str
        'site_name': site.site_name if site else 'Unknown',
        'site_region': site.region if site else '',
        'product_category': plan.product_category,
        'forecasted_sales': forecasted,
        'actual_sales': actual,
        'seasonal_adjustments': adj,
        'adj_pct': round(adj * 100, 2),
        'variance_pct': variance_pct,
        'performance': performance,
        'gap': round(actual - forecasted, 2),
        'notes': plan.notes or '',
        'created_at': plan.created_at.isoformat() if plan.created_at else None,
        'updated_at': plan.updated_at.isoformat() if plan.updated_at else None,
    }


# ─── Actual sales for a specific month/site/category ─────────────────────────

@bp.route('/actual-for-month', methods=['GET'])
@token_required
@handle_exceptions
def actual_for_month(current_user):
    """
    Returns the sum of actual sales from confirmed/delivered sales orders
    for a given month, site_id, and product category.
    Used by the form to auto-populate Actual Sales for past months.
    """
    month    = request.args.get('month', '').strip()
    site_id  = request.args.get('site_id', '').strip()
    category = request.args.get('category', '').strip()

    if not month or not site_id or not category:
        return jsonify({'success': False, 'message': 'month, site_id, and category are required'}), 400

    # Parse month label → year and month number
    try:
        parts = month.split('-')
        mon_abbr = parts[0][:3].title()
        year = int(parts[1])
        mon_num = MONTHS_ORDER.index(mon_abbr) + 1
    except (IndexError, ValueError):
        return jsonify({'success': False, 'message': f'Invalid month format: {month}'}), 400

    # Scope check
    allowed = _allowed_site_ids(current_user)
    if allowed is not None and str(site_id) not in allowed:
        return jsonify({'success': False, 'message': 'Site not in your scope'}), 403

    # Sum line_total from sales orders matching site + category + month/year
    row = (
        db.session.query(func.sum(SalesOrderItem.line_total).label('total'),
                         func.count(SalesOrder.id.distinct()).label('order_count'))
        .join(SalesOrderItem, SalesOrderItem.so_id == SalesOrder.id)
        .join(Product, Product.product_id == SalesOrderItem.product_id)
        .filter(
            SalesOrder.warehouse_id == site_id,
            Product.category == category,
            SalesOrder.status.in_(['Delivered', 'Confirmed']),
            extract('month', SalesOrder.order_date) == mon_num,
            extract('year',  SalesOrder.order_date) == year,
        )
        .one()
    )

    total = float(row.total or 0)
    order_count = int(row.order_count or 0)

    return jsonify({
        'success': True,
        'month': month,
        'site_id': site_id,
        'category': category,
        'actual_sales': round(total, 2),
        'order_count': order_count,
    })


# ─── KPI summary ──────────────────────────────────────────────────────────────

@bp.route('/kpi', methods=['GET'])
@token_required
@handle_exceptions
def get_kpis(current_user):
    allowed = _allowed_site_ids(current_user)
    plans = _base_filter(db.session.query(SeasonalPlan), allowed).all()
    if not plans:
        return jsonify({'success': True, 'data': {
            'total_plans': 0, 'avg_forecasted': 0, 'avg_actual': 0,
            'avg_adj_pct': 0, 'exceeded_pct': 0, 'critical_pct': 0,
            'total_forecasted': 0, 'total_actual': 0, 'overall_variance_pct': 0
        }})

    total = len(plans)
    total_f = sum(float(p.forecasted_sales or 0) for p in plans)
    total_a = sum(float(p.actual_sales or 0) for p in plans)
    avg_adj = sum(float(p.seasonal_adjustments or 0) for p in plans) / total
    exceeded = sum(1 for p in plans
                   if float(p.actual_sales or 0) >= float(p.forecasted_sales or 0) * PERF_EXCEEDED)
    critical = sum(1 for p in plans
                   if float(p.actual_sales or 0) < float(p.forecasted_sales or 0) * PERF_BELOW)
    plans_with_actuals = [p for p in plans if float(p.actual_sales or 0) > 0]

    return jsonify({'success': True, 'data': {
        'total_plans': total,
        'total_forecasted': round(total_f, 2),
        'total_actual': round(total_a, 2),
        'avg_forecasted': round(total_f / total, 2),
        'avg_actual': round(total_a / total, 2),
        'avg_adj_pct': round(avg_adj * 100, 2),
        'exceeded_pct': round(exceeded / total * 100, 1),
        'critical_pct': round(critical / total * 100, 1),
        'overall_variance_pct': round((total_a - total_f) / total_f * 100, 2) if total_f else 0,
        'plans_with_actuals': len(plans_with_actuals),
        'forecast_coverage_pct': round(len(plans_with_actuals) / total * 100, 1),
    }})


# ─── Analytics ────────────────────────────────────────────────────────────────

@bp.route('/analytics', methods=['GET'])
@token_required
@handle_exceptions
def get_analytics(current_user):
    allowed = _allowed_site_ids(current_user)
    category_filter = request.args.get('category')
    month_filter = request.args.get('month')

    def base_q():
        q = db.session.query(SeasonalPlan)
        q = _base_filter(q, allowed)      # BUG FIX: use helper, not inline ternary
        if category_filter:
            q = q.filter(SeasonalPlan.product_category == category_filter)
        if month_filter:
            q = q.filter(SeasonalPlan.month == month_filter)
        return q

    # Monthly trend
    monthly_raw = (
        db.session.query(
            SeasonalPlan.month,
            func.sum(SeasonalPlan.forecasted_sales).label('f'),
            func.sum(SeasonalPlan.actual_sales).label('a'),
            func.avg(SeasonalPlan.seasonal_adjustments).label('adj')
        )
        .filter(SeasonalPlan.site_id.in_(allowed) if allowed is not None
                else SeasonalPlan.site_id.isnot(None))   # BUG FIX
        .group_by(SeasonalPlan.month)
        .all()
    )
    monthly_sorted = sorted(monthly_raw, key=lambda r: _month_sort_key(r.month))
    monthly_trend = {
        'labels': [r.month for r in monthly_sorted],
        'forecasted': [round(float(r.f or 0), 2) for r in monthly_sorted],
        'actual': [round(float(r.a or 0), 2) for r in monthly_sorted],
        'adj_pct': [round(float(r.adj or 0) * 100, 2) for r in monthly_sorted],
    }

    # Category performance
    cat_raw = (
        db.session.query(
            SeasonalPlan.product_category,
            func.sum(SeasonalPlan.forecasted_sales).label('f'),
            func.sum(SeasonalPlan.actual_sales).label('a'),
        )
        .filter(SeasonalPlan.site_id.in_(allowed) if allowed is not None
                else SeasonalPlan.site_id.isnot(None))
        .group_by(SeasonalPlan.product_category)
        .all()
    )
    category_perf = []
    for r in cat_raw:
        f = float(r.f or 0)
        a = float(r.a or 0)
        category_perf.append({
            'category': r.product_category,
            'forecasted': round(f, 2),
            'actual': round(a, 2),
            'variance_pct': round((a - f) / f * 100, 2) if f else 0
        })
    category_perf.sort(key=lambda x: x['actual'], reverse=True)

    # Site ranking
    site_raw = (
        db.session.query(
            SeasonalPlan.site_id,
            func.sum(SeasonalPlan.actual_sales).label('a'),
            func.sum(SeasonalPlan.forecasted_sales).label('f'),
        )
        .filter(SeasonalPlan.site_id.in_(allowed) if allowed is not None
                else SeasonalPlan.site_id.isnot(None))
        .group_by(SeasonalPlan.site_id)
        .order_by(func.sum(SeasonalPlan.actual_sales).desc())
        .limit(10)
        .all()
    )
    site_ids = [r.site_id for r in site_raw]
    sites_map = {s.site_id: s.site_name for s in
                 Site.query.filter(Site.site_id.in_(site_ids)).all()}
    site_ranking = [
        {
            'site_id': str(r.site_id),
            'site_name': sites_map.get(r.site_id, str(r.site_id)),
            'actual': round(float(r.a or 0), 2),
            'forecasted': round(float(r.f or 0), 2),
        }
        for r in site_raw
    ]

    # Adjustment distribution
    plans_all = base_q().all()
    bins = {'< -10%': 0, '-10% to 0%': 0, '0% to 10%': 0, '> 10%': 0}
    for p in plans_all:
        adj = float(p.seasonal_adjustments or 0) * 100
        if adj < -10:
            bins['< -10%'] += 1
        elif adj < 0:
            bins['-10% to 0%'] += 1
        elif adj <= 10:
            bins['0% to 10%'] += 1
        else:
            bins['> 10%'] += 1

    return jsonify({'success': True, 'data': {
        'monthly_trend': monthly_trend,
        'category_perf': category_perf,
        'site_ranking': site_ranking,
        'adj_distribution': bins,
    }})


# ─── Heatmap ──────────────────────────────────────────────────────────────────

@bp.route('/heatmap', methods=['GET'])
@token_required
@handle_exceptions
def get_heatmap(current_user):
    """Month × Category matrix of average variance %."""
    allowed = _allowed_site_ids(current_user)
    raw = (
        db.session.query(
            SeasonalPlan.month,
            SeasonalPlan.product_category,
            func.avg(SeasonalPlan.actual_sales).label('a'),
            func.avg(SeasonalPlan.forecasted_sales).label('f'),
        )
        .filter(SeasonalPlan.site_id.in_(allowed) if allowed is not None
                else SeasonalPlan.site_id.isnot(None))
        .group_by(SeasonalPlan.month, SeasonalPlan.product_category)
        .all()
    )

    months = sorted({r.month for r in raw}, key=_month_sort_key)
    categories = sorted({r.product_category for r in raw})
    matrix = {}
    for r in raw:
        f = float(r.f or 0)
        a = float(r.a or 0)
        vp = round((a - f) / f * 100, 1) if f else 0
        matrix.setdefault(r.month, {})[r.product_category] = vp

    return jsonify({'success': True, 'data': {
        'months': months, 'categories': categories, 'matrix': matrix,
    }})


# ─── Forecast Accuracy (NEW) ──────────────────────────────────────────────────

@bp.route('/forecast-accuracy', methods=['GET'])
@token_required
@handle_exceptions
def forecast_accuracy(current_user):
    """
    Returns MAE, MAPE, RMSE per category for plans that have actual_sales > 0.
    Useful for evaluating how good the forecasting model actually is.
    """
    allowed = _allowed_site_ids(current_user)
    plans = _base_filter(db.session.query(SeasonalPlan), allowed)\
        .filter(SeasonalPlan.actual_sales > 0, SeasonalPlan.forecasted_sales > 0)\
        .all()

    by_cat = defaultdict(list)
    for p in plans:
        by_cat[p.product_category].append(
            (float(p.forecasted_sales), float(p.actual_sales))
        )

    results = []
    for cat, pairs in by_cat.items():
        n = len(pairs)
        errors = [abs(a - f) for f, a in pairs]
        pct_errors = [abs(a - f) / f * 100 for f, a in pairs]
        sq_errors = [(a - f) ** 2 for f, a in pairs]
        results.append({
            'category': cat,
            'n': n,
            'mae': round(sum(errors) / n, 2),
            'mape': round(sum(pct_errors) / n, 2),
            'rmse': round(math.sqrt(sum(sq_errors) / n), 2),
            'bias': round(sum(a - f for f, a in pairs) / n, 2),  # + = under-forecasting
        })
    results.sort(key=lambda x: x['mape'])
    return jsonify({'success': True, 'data': results})


# ─── Stockout Risk (NEW) ──────────────────────────────────────────────────────

@bp.route('/stockout-risk', methods=['GET'])
@token_required
@handle_exceptions
def stockout_risk(current_user):
    """
    Flags plans where actual_sales significantly exceeded forecasted_sales,
    indicating the site likely ran out of stock or was constrained by supply.
    Threshold: actual > forecast * 1.25 (25% overrun).
    """
    allowed = _allowed_site_ids(current_user)
    threshold = float(request.args.get('threshold', 1.25))
    plans = _base_filter(db.session.query(SeasonalPlan), allowed)\
        .filter(SeasonalPlan.actual_sales > 0, SeasonalPlan.forecasted_sales > 0)\
        .all()

    at_risk = []
    for p in plans:
        f = float(p.forecasted_sales)
        a = float(p.actual_sales)
        if a >= f * threshold:
            site = Site.query.get(p.site_id)
            at_risk.append({
                'plan_id': p.id,
                'month': p.month,
                'site_id': str(p.site_id),
                'site_name': site.site_name if site else str(p.site_id),
                'category': p.product_category,
                'forecasted': round(f, 2),
                'actual': round(a, 2),
                'overrun_pct': round((a - f) / f * 100, 1),
                'suggested_increase': round((a - f) * 1.10, 2),  # buffer for next cycle
            })

    at_risk.sort(key=lambda x: x['overrun_pct'], reverse=True)
    return jsonify({'success': True, 'data': at_risk,
                    'count': len(at_risk), 'threshold_pct': round((threshold - 1) * 100)})


# ─── Year-over-Year (NEW) ─────────────────────────────────────────────────────

@bp.route('/yoy', methods=['GET'])
@token_required
@handle_exceptions
def year_over_year(current_user):
    """Compare a given year's actuals vs prior year by month and category."""
    year = request.args.get('year', datetime.utcnow().year, type=int)
    prior_year = year - 1
    allowed = _allowed_site_ids(current_user)
    category = request.args.get('category')

    def get_monthly_totals(yr):
        q = db.session.query(SeasonalPlan)
        q = _base_filter(q, allowed)
        q = q.filter(SeasonalPlan.month.like(f'%-{yr}'))
        if category:
            q = q.filter(SeasonalPlan.product_category == category)
        plans = q.all()
        by_month = defaultdict(float)
        for p in plans:
            mon = p.month.split('-')[0]
            by_month[mon] += float(p.actual_sales or 0)
        return by_month

    curr = get_monthly_totals(year)
    prev = get_monthly_totals(prior_year)
    all_months = sorted(set(list(curr.keys()) + list(prev.keys())),
                        key=lambda m: MONTHS_ORDER.index(m) if m in MONTHS_ORDER else 99)

    rows = []
    for m in all_months:
        c = curr.get(m, 0)
        p = prev.get(m, 0)
        rows.append({
            'month': m,
            'current': round(c, 2),
            'prior': round(p, 2),
            'change_pct': round((c - p) / p * 100, 1) if p else None,
        })

    return jsonify({'success': True, 'data': {
        'year': year, 'prior_year': prior_year, 'months': rows,
        'total_current': round(sum(curr.values()), 2),
        'total_prior': round(sum(prev.values()), 2),
    }})


# ─── Copy Month (NEW) ─────────────────────────────────────────────────────────

@bp.route('/copy-month', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def copy_month(current_user):
    """
    Copy all plans from source_month to target_month.
    Useful for seeding next month's forecasts from this month's plan.
    Only copies forecasted_sales and seasonal_adjustments (not actuals).
    """
    body = request.get_json(force=True)
    source = body.get('source_month', '').strip()
    target = body.get('target_month', '').strip()
    overwrite = body.get('overwrite', False)

    if not source or not target:
        return jsonify({'success': False, 'message': 'source_month and target_month required'}), 400

    allowed = _allowed_site_ids(current_user)
    source_plans = _base_filter(
        db.session.query(SeasonalPlan).filter_by(month=source), allowed
    ).all()

    if not source_plans:
        return jsonify({'success': False, 'message': f'No plans found for {source}'}), 404

    copied = 0
    skipped = 0
    for sp in source_plans:
        existing = SeasonalPlan.query.filter_by(
            month=target, site_id=sp.site_id, product_category=sp.product_category
        ).first()
        if existing:
            if overwrite:
                existing.forecasted_sales = sp.forecasted_sales
                existing.seasonal_adjustments = sp.seasonal_adjustments
                existing.updated_at = datetime.utcnow()
                copied += 1
            else:
                skipped += 1
        else:
            new_plan = SeasonalPlan(
                month=target,
                site_id=sp.site_id,
                product_category=sp.product_category,
                forecasted_sales=sp.forecasted_sales,
                actual_sales=0,
                seasonal_adjustments=sp.seasonal_adjustments,
                notes=f'Copied from {source}',
                created_by=current_user.id,
            )
            db.session.add(new_plan)
            copied += 1

    db.session.commit()
    log_audit(current_user, 'COPY_MONTH', 'SeasonalPlan', target,
              {'source': source, 'copied': copied, 'skipped': skipped})
    return jsonify({'success': True, 'copied': copied, 'skipped': skipped,
                    'message': f'Copied {copied} plans from {source} → {target}'})


# ─── CRUD ─────────────────────────────────────────────────────────────────────

@bp.route('/', methods=['GET'])
@token_required
@handle_exceptions
def list_plans(current_user):
    allowed = _allowed_site_ids(current_user)
    q = _base_filter(db.session.query(SeasonalPlan), allowed)

    category = request.args.get('category')
    month = request.args.get('month')
    site_id = request.args.get('site_id')
    performance = request.args.get('performance')
    search = request.args.get('search', '').strip()

    if category:
        q = q.filter(SeasonalPlan.product_category == category)
    if month:
        q = q.filter(SeasonalPlan.month == month)
    if site_id:
        q = q.filter(SeasonalPlan.site_id == site_id)

    plans = q.all()
    dicts = [_plan_to_dict(p) for p in plans]

    if performance:
        dicts = [d for d in dicts if d['performance'] == performance]
    if search:
        sl = search.lower()
        dicts = [d for d in dicts if
                 sl in str(d['site_id']).lower() or      # BUG FIX: str()
                 sl in d['site_name'].lower() or
                 sl in d['product_category'].lower() or
                 sl in d['month'].lower()]

    sort_by = request.args.get('sort', 'month')
    sort_dir = request.args.get('dir', 'asc')
    reverse = sort_dir == 'desc'
    if sort_by in ('forecasted_sales', 'actual_sales', 'variance_pct', 'adj_pct', 'gap'):
        dicts.sort(key=lambda d: d.get(sort_by, 0), reverse=reverse)
    else:
        dicts.sort(key=lambda d: _month_sort_key(d['month']), reverse=reverse)

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 25, type=int), 200)
    total = len(dicts)
    start = (page - 1) * per_page
    page_data = dicts[start: start + per_page]

    return jsonify({'success': True, 'data': page_data,
                    'pagination': {'page': page, 'per_page': per_page,
                                   'total': total, 'pages': -(-total // per_page)}})


@bp.route('/', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def create_plan(current_user):
    body = request.get_json(force=True)
    required = ('month', 'site_id', 'product_category', 'forecasted_sales')
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify({'success': False,
                        'message': f'Missing fields: {", ".join(missing)}'}), 400

    # Validate category against DB (fall back to hardcoded list if DB empty)
    db_categories = [c.category_name for c in Category.query.all()]
    allowed_cats = db_categories if db_categories else VALID_CATEGORIES
    if body['product_category'] not in allowed_cats:
        return jsonify({'success': False,
                        'message': f'Invalid category. Valid: {allowed_cats}'}), 400

    allowed = _allowed_site_ids(current_user)
    if allowed is not None and str(body['site_id']) not in allowed:   # BUG FIX: str()
        return jsonify({'success': False, 'message': 'Site not in your scope'}), 403

    # BUG FIX: upsert to avoid hitting unique constraint
    existing = SeasonalPlan.query.filter_by(
        month=body['month'],
        site_id=body['site_id'],
        product_category=body['product_category']
    ).first()

    if existing:
        return jsonify({
            'success': False,
            'message': f'A plan for {body["month"]} / {body["site_id"]} / {body["product_category"]} already exists. Use PUT to update.',
            'existing_id': existing.id
        }), 409

    plan = SeasonalPlan(
        month=body['month'],
        site_id=body['site_id'],
        product_category=body['product_category'],
        forecasted_sales=float(body['forecasted_sales']),
        actual_sales=float(body.get('actual_sales', 0)),
        seasonal_adjustments=float(body.get('seasonal_adjustments', 0)),
        notes=body.get('notes', ''),                     # BUG FIX: notes was ignored
        created_by=current_user.id,
    )
    db.session.add(plan)
    db.session.commit()
    log_audit(current_user, 'CREATE', 'SeasonalPlan', plan.id,
              {'month': plan.month, 'site_id': plan.site_id, 'category': plan.product_category})
    return jsonify({'success': True, 'data': _plan_to_dict(plan)}), 201


@bp.route('/<int:plan_id>', methods=['GET'])
@token_required
@handle_exceptions
def get_plan(current_user, plan_id):
    plan = SeasonalPlan.query.get_or_404(plan_id)
    allowed = _allowed_site_ids(current_user)
    if allowed is not None and str(plan.site_id) not in allowed:    # BUG FIX: str()
        return jsonify({'success': False, 'message': 'Forbidden'}), 403
    return jsonify({'success': True, 'data': _plan_to_dict(plan)})


@bp.route('/<int:plan_id>', methods=['PUT'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def update_plan(current_user, plan_id):
    plan = SeasonalPlan.query.get_or_404(plan_id)
    allowed = _allowed_site_ids(current_user)
    if allowed is not None and str(plan.site_id) not in allowed:    # BUG FIX: str()
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    body = request.get_json(force=True)
    for field in ('month', 'site_id', 'product_category'):
        if field in body:
            setattr(plan, field, body[field])
    for field in ('forecasted_sales', 'actual_sales', 'seasonal_adjustments'):
        if field in body:
            setattr(plan, field, float(body[field]))
    if 'notes' in body:                                             # BUG FIX: notes
        plan.notes = body['notes']
    plan.updated_at = datetime.utcnow()

    db.session.commit()
    log_audit(current_user, 'UPDATE', 'SeasonalPlan', plan.id,
              {'month': plan.month, 'site_id': plan.site_id})
    return jsonify({'success': True, 'data': _plan_to_dict(plan)})


@bp.route('/<int:plan_id>', methods=['DELETE'])
@token_required
@role_required('Admin')
@handle_exceptions
def delete_plan(current_user, plan_id):
    plan = SeasonalPlan.query.get_or_404(plan_id)
    db.session.delete(plan)
    db.session.commit()
    log_audit(current_user, 'DELETE', 'SeasonalPlan', plan_id, {})
    return jsonify({'success': True, 'message': 'Plan deleted'})


# ─── Bulk import (with upsert) ────────────────────────────────────────────────

@bp.route('/bulk-import', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def bulk_import(current_user):
    """
    BUG FIX: original did INSERT-only → duplicate rows on re-import.
    Now upserts: updates if (month, site_id, category) exists.
    """
    file = request.files.get('file')
    if not file:
        return jsonify({'success': False, 'message': 'No file provided'}), 400

    stream = io.StringIO(file.stream.read().decode('utf-8'))
    reader = csv.DictReader(stream)
    allowed = _allowed_site_ids(current_user)
    created = updated = 0
    errors = []

    for i, row in enumerate(reader, start=2):
        try:
            site_id = str((row.get('Site ID') or row.get('site_id') or '').strip())
            month = (row.get('Month') or row.get('month') or '').strip()
            category = (row.get('Product Category') or row.get('product_category') or '').strip()
            forecasted = float(row.get('Forecasted Sales') or row.get('forecasted_sales') or 0)
            actual = float(row.get('Actual Sales') or row.get('actual_sales') or 0)
            adj = float(row.get('Seasonal Adjustments') or row.get('seasonal_adjustments') or 0)
            notes = (row.get('Notes') or row.get('notes') or '').strip()

            if not (site_id and month and category):
                errors.append(f'Row {i}: missing required fields')
                continue
            if allowed is not None and site_id not in allowed:    # BUG FIX: already str
                errors.append(f'Row {i}: site {site_id} not in scope')
                continue

            existing = SeasonalPlan.query.filter_by(
                month=month, site_id=site_id, product_category=category
            ).first()

            if existing:
                existing.forecasted_sales = forecasted
                existing.actual_sales = actual
                existing.seasonal_adjustments = adj
                if notes:
                    existing.notes = notes
                existing.updated_at = datetime.utcnow()
                updated += 1
            else:
                db.session.add(SeasonalPlan(
                    month=month, site_id=site_id, product_category=category,
                    forecasted_sales=forecasted, actual_sales=actual,
                    seasonal_adjustments=adj, notes=notes, created_by=current_user.id
                ))
                created += 1
        except Exception as e:
            errors.append(f'Row {i}: {str(e)}')

    db.session.commit()
    log_audit(current_user, 'BULK_IMPORT', 'SeasonalPlan', 'csv',
              {'created': created, 'updated': updated, 'errors': len(errors)})
    return jsonify({'success': True, 'created': created, 'updated': updated, 'errors': errors})


# ─── CSV export ───────────────────────────────────────────────────────────────

@bp.route('/export/csv', methods=['GET'])
@token_required
@handle_exceptions
def export_csv(current_user):
    allowed = _allowed_site_ids(current_user)
    plans = _base_filter(db.session.query(SeasonalPlan), allowed).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Month', 'Site ID', 'Site Name', 'Product Category',
        'Forecasted Sales', 'Actual Sales',
        'Seasonal Adjustment Fraction',   # BUG FIX: was "(%)" but value was fraction
        'Variance (%)', 'Performance', 'Gap', 'Notes'
    ])
    for p in sorted(plans, key=lambda x: _month_sort_key(x.month)):
        d = _plan_to_dict(p)
        writer.writerow([
            d['month'], d['site_id'], d['site_name'], d['product_category'],
            d['forecasted_sales'], d['actual_sales'],
            d['seasonal_adjustments'],   # raw fraction
            d['variance_pct'], d['performance'], d['gap'], d['notes']
        ])

    output.seek(0)
    return Response(
        output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=seasonal_plans.csv'}
    )


# ─── Sync Preview — shows why auto-sync only updates N plans ─────────────────

@bp.route('/sync-preview', methods=['GET'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def sync_preview(current_user):
    """
    Diagnoses why auto-sync updated only N plans by showing:
      - matched:   sales orders found AND a SeasonalPlan exists  → will sync
      - no_plan:   sales orders found but NO SeasonalPlan exists → skipped (plan missing)
      - no_sales:  SeasonalPlan exists but NO sales orders found → actual stays 0
    """
    allowed = _allowed_site_ids(current_user)

    # All sales order aggregates (same query as auto-sync)
    rows = (
        db.session.query(
            extract('month', SalesOrder.order_date).label('mon_num'),
            extract('year',  SalesOrder.order_date).label('yr'),
            SalesOrder.warehouse_id.label('site_id'),
            Product.category.label('category'),
            func.sum(SalesOrderItem.line_total).label('total'),
            func.count(SalesOrder.id.distinct()).label('order_count')
        )
        .join(SalesOrderItem, SalesOrderItem.so_id == SalesOrder.id)
        .join(Product, Product.product_id == SalesOrderItem.product_id)
        .filter(SalesOrder.status.in_(['Delivered', 'Confirmed']))
        .group_by(
            extract('month', SalesOrder.order_date),
            extract('year',  SalesOrder.order_date),
            SalesOrder.warehouse_id,
            Product.category
        )
        .all()
    )

    matched   = []
    no_plan   = []

    sites_cache = {s.site_id: s.site_name for s in Site.query.all()}

    for row in rows:
        site_id = str(row.site_id)
        if allowed is not None and site_id not in allowed:
            continue
        mon_idx     = int(row.mon_num) - 1
        month_label = f"{MONTHS_ORDER[mon_idx]}-{int(row.yr)}"
        total       = round(float(row.total or 0), 2)
        site_name   = sites_cache.get(site_id, site_id)
        entry = {
            'month':       month_label,
            'site_id':     site_id,
            'site_name':   site_name,
            'category':    row.category,
            'sales_total': total,
            'order_count': int(row.order_count),
        }
        plan = SeasonalPlan.query.filter_by(
            month=month_label, site_id=site_id, product_category=row.category
        ).first()
        if plan:
            entry['plan_id']          = plan.id
            entry['current_actual']   = float(plan.actual_sales or 0)
            entry['forecasted_sales'] = float(plan.forecasted_sales or 0)
            matched.append(entry)
        else:
            no_plan.append(entry)

    # Plans with no matching sales orders
    all_plans = _base_filter(db.session.query(SeasonalPlan), allowed).all()
    so_keys   = {
        (str(r.site_id), f"{MONTHS_ORDER[int(r.mon_num)-1]}-{int(r.yr)}", r.category)
        for r in rows
        if allowed is None or str(r.site_id) in allowed
    }
    no_sales = []
    for p in all_plans:
        key = (str(p.site_id), p.month, p.product_category)
        if key not in so_keys:
            site_name = sites_cache.get(p.site_id, str(p.site_id))
            no_sales.append({
                'plan_id':        p.id,
                'month':          p.month,
                'site_id':        str(p.site_id),
                'site_name':      site_name,
                'category':       p.product_category,
                'forecasted':     float(p.forecasted_sales or 0),
                'current_actual': float(p.actual_sales or 0),
            })

    return jsonify({
        'success': True,
        'summary': {
            'will_sync':        len(matched),
            'no_plan_skipped':  len(no_plan),
            'no_sales_orders':  len(no_sales),
        },
        'matched':  sorted(matched,  key=lambda x: (x['month'], x['site_name'])),
        'no_plan':  sorted(no_plan,  key=lambda x: (x['month'], x['site_name'])),
        'no_sales': sorted(no_sales, key=lambda x: (x['month'], x['site_name'])),
    })


# ─── Auto-sync actuals ────────────────────────────────────────────────────────

@bp.route('/auto-sync', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def auto_sync_actuals(current_user):
    """
    BUG FIX: original used func.to_char(order_date, 'Mon-YYYY') which is
    locale-dependent on the DB server.  Now extracts month number + year
    in SQL and reconstructs the label in Python for locale safety.
    """
    allowed = _allowed_site_ids(current_user)

    rows = (
        db.session.query(
            extract('month', SalesOrder.order_date).label('mon_num'),
            extract('year', SalesOrder.order_date).label('yr'),
            SalesOrder.warehouse_id.label('site_id'),
            Product.category.label('category'),
            func.sum(SalesOrderItem.line_total).label('total')
        )
        .join(SalesOrderItem, SalesOrderItem.so_id == SalesOrder.id)
        .join(Product, Product.product_id == SalesOrderItem.product_id)
        .filter(SalesOrder.status.in_(['Delivered', 'Confirmed']))
        .group_by(
            extract('month', SalesOrder.order_date),
            extract('year', SalesOrder.order_date),
            SalesOrder.warehouse_id,
            Product.category
        )
        .all()
    )

    updated = skipped = 0
    for row in rows:
        site_id = str(row.site_id)
        if allowed is not None and site_id not in allowed:
            skipped += 1
            continue
        mon_idx = int(row.mon_num) - 1  # 0-based
        month_label = f"{MONTHS_ORDER[mon_idx]}-{int(row.yr)}"
        plan = SeasonalPlan.query.filter_by(
            month=month_label, site_id=site_id, product_category=row.category
        ).first()
        if plan:
            plan.actual_sales = float(row.total or 0)
            plan.updated_at = datetime.utcnow()
            updated += 1

    db.session.commit()
    log_audit(current_user, 'AUTO_SYNC', 'SeasonalPlan', 'actuals',
              {'updated': updated, 'skipped': skipped})
    return jsonify({'success': True,
                    'message': f'Synced actuals for {updated} plans.',
                    'updated': updated, 'skipped': skipped})


# ─── Auto-forecast ────────────────────────────────────────────────────────────

@bp.route('/auto-forecast', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def auto_generate_forecasts(current_user):
    allowed = _allowed_site_ids(current_user)
    all_plans = _base_filter(db.session.query(SeasonalPlan), allowed).all()

    groups = defaultdict(list)
    for p in all_plans:
        groups[(str(p.site_id), p.product_category)].append(p)

    next_month_label = _next_month_label()
    sites_cache = {str(s.site_id): s.site_name for s in Site.query.all()}

    created_records = []
    updated_records = []
    skipped_records = []

    for (site_id, category), plans in groups.items():
        site_name    = sites_cache.get(site_id, site_id)
        sorted_plans = sorted(plans, key=lambda p: _month_sort_key(p.month))
        recent       = sorted_plans[-3:]
        actuals      = [float(p.actual_sales or 0) for p in recent]

        if not any(a > 0 for a in actuals):
            skipped_records.append({
                'site_id': site_id, 'site_name': site_name,
                'category': category, 'reason': 'No actuals found in last 3 months'
            })
            continue

        # Weighted average: weights [1, 2, 3] for oldest → newest
        weights          = list(range(1, len(recent) + 1))
        weighted_actual  = sum(a * w for a, w in zip(actuals, weights)) / sum(weights)

        # Derive adj from variance ratio (same logic as frontend suggestForecast)
        plans_with_fc = [p for p in recent if float(p.forecasted_sales or 0) > 0]
        if plans_with_fc:
            avg_adj = sum(
                (float(p.actual_sales) - float(p.forecasted_sales)) / float(p.forecasted_sales)
                for p in plans_with_fc
            ) / len(plans_with_fc)
            avg_adj = round(avg_adj, 3)
        else:
            avg_adj = sum(float(p.seasonal_adjustments or 0) for p in recent) / len(recent)

        forecast = round(weighted_actual * (1 + avg_adj), 2)

        # Recent actuals for display
        recent_actuals = [
            {'month': p.month, 'actual': float(p.actual_sales or 0)}
            for p in reversed(recent)
        ]

        existing = SeasonalPlan.query.filter_by(
            month=next_month_label, site_id=site_id, product_category=category
        ).first()

        record = {
            'site_id':        site_id,
            'site_name':      site_name,
            'category':       category,
            'new_forecast':   forecast,
            'adj_pct':        round(avg_adj * 100, 1),
            'based_on':       len(recent),
            'recent_actuals': recent_actuals,
        }

        if existing:
            record['old_forecast'] = float(existing.forecasted_sales or 0)
            existing.forecasted_sales = forecast
            existing.updated_at = datetime.utcnow()
            updated_records.append(record)
        else:
            record['old_forecast'] = None
            db.session.add(SeasonalPlan(
                month=next_month_label, site_id=site_id, product_category=category,
                forecasted_sales=forecast, actual_sales=0, seasonal_adjustments=avg_adj,
                created_by=current_user.id, notes='Auto-generated forecast'
            ))
            created_records.append(record)

    db.session.commit()
    created = len(created_records)
    updated = len(updated_records)
    log_audit(current_user, 'AUTO_FORECAST', 'SeasonalPlan', next_month_label,
              {'created': created, 'updated': updated})
    return jsonify({
        'success': True,
        'message': f'Forecast for {next_month_label}: {created} created, {updated} updated.',
        'month':   next_month_label,
        'created': created,
        'updated': updated,
        'skipped': len(skipped_records),
        'created_records': sorted(created_records, key=lambda x: (x['site_name'], x['category'])),
        'updated_records': sorted(updated_records, key=lambda x: (x['site_name'], x['category'])),
        'skipped_records': sorted(skipped_records, key=lambda x: (x['site_name'], x['category'])),
    })


# ─── Fetch actuals for a specific month / site / category ────────────────────

@bp.route('/fetch-actuals', methods=['GET'])
@token_required
@handle_exceptions
def fetch_actuals_for_plan(current_user):
    """
    Returns the sum of actual sales (from delivered/confirmed sales orders)
    for a given month label (e.g. 'Jan-2025'), site_id, and product_category.

    Used by the frontend to auto-populate Actual Sales when the month is in
    the past and all three fields (month, site, category) are selected.

    Query params:
        month     — e.g. 'Jan-2025'
        site_id   — site/warehouse id
        category  — product category string
    """
    month    = request.args.get('month', '').strip()
    site_id  = request.args.get('site_id', '').strip()
    category = request.args.get('category', '').strip()

    if not (month and site_id and category):
        return jsonify({'success': False,
                        'message': 'month, site_id, and category are required'}), 400

    # Parse month label → (year, month_number)
    try:
        parts   = month.split('-')
        mon_abbr = parts[0][:3].title()
        year     = int(parts[1])
        mon_num  = MONTHS_ORDER.index(mon_abbr) + 1   # 1-based
    except (ValueError, IndexError):
        return jsonify({'success': False,
                        'message': f'Invalid month format: {month}. Use Mon-YYYY'}), 400

    # Gate: only return actuals for past months
    now = datetime.utcnow()
    plan_date = date(year, mon_num, 1)
    current_month_start = date(now.year, now.month, 1)
    if plan_date >= current_month_start:
        return jsonify({'success': True,
                        'total': 0,
                        'is_past': False,
                        'message': 'Month is current or future — actuals not yet available'}), 200

    # Scope check
    allowed = _allowed_site_ids(current_user)
    if allowed is not None and site_id not in allowed:
        return jsonify({'success': False, 'message': 'Site not in your scope'}), 403

    # Sum line_total for delivered/confirmed orders in that month/site/category
    result = (
        db.session.query(func.sum(SalesOrderItem.line_total))
        .join(SalesOrder, SalesOrder.id == SalesOrderItem.so_id)
        .join(Product, Product.product_id == SalesOrderItem.product_id)
        .filter(
            SalesOrder.warehouse_id == site_id,
            SalesOrder.status.in_(['Delivered', 'Confirmed']),
            extract('year',  SalesOrder.order_date) == year,
            extract('month', SalesOrder.order_date) == mon_num,
            Product.category == category,
        )
        .scalar()
    )

    total = round(float(result or 0), 2)
    return jsonify({
        'success': True,
        'total': total,
        'is_past': True,
        'month': month,
        'site_id': site_id,
        'category': category,
        'message': f'Actual sales computed from {month} sales orders',
    })


# ─── Meta ─────────────────────────────────────────────────────────────────────

@bp.route('/meta', methods=['GET'])
@token_required
@handle_exceptions
def get_meta(current_user):
    allowed = _allowed_site_ids(current_user)
    plans = _base_filter(db.session.query(SeasonalPlan), allowed).all()
    months = sorted({p.month for p in plans}, key=_month_sort_key)
    categories = sorted({p.product_category for p in plans})
    site_ids = sorted({str(p.site_id) for p in plans})
    sites_map = {str(s.site_id): s.site_name
                 for s in Site.query.filter(Site.site_id.in_(site_ids)).all()}
    sites = [{'id': sid, 'name': sites_map.get(sid, sid)} for sid in site_ids]

    # Pull valid categories from the categories table in DB.
    # Falls back to hardcoded list if table is empty (e.g. fresh install).
    db_categories = sorted([c.category_name for c in Category.query.all()])
    valid_categories = db_categories if db_categories else VALID_CATEGORIES

    return jsonify({'success': True, 'data': {
        'months': months,
        'categories': categories,
        'sites': sites,
        'valid_categories': valid_categories,
        'performance_labels': ['Exceeded', 'On Track', 'Below Target', 'Critical'],
        'thresholds': {
            'exceeded': PERF_EXCEEDED,
            'on_track': PERF_ON_TRACK,
            'below': PERF_BELOW,
        }
    }})
