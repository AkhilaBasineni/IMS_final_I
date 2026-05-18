import pandas as pd
import sys
import os
from datetime import datetime, date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')


def get_file_path(filename):
    return os.path.join(DATA_DIR, filename)


def safe_int(val):
    try:
        return int(val) if pd.notna(val) else None
    except:
        return None


def safe_float(val):
    try:
        return float(val) if pd.notna(val) else None
    except:
        return None


def safe_date(val):
    try:
        return pd.to_datetime(val, dayfirst=True).date() if pd.notna(val) else None
    except:
        return None


def safe_str(val):
    return str(val).strip() if pd.notna(val) else None


def clean_df(df):
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
    return df


def find_column(df, search_terms):
    for col in df.columns:
        for term in search_terms:
            if term in col:
                return col
    return None


# -----------------------------------------------------------------
#  INCONSISTENCY HANDLING: Inventory Data
# -----------------------------------------------------------------
def clean_inventory_data(inv_df, sales_df):
    """
    Resolves 3 known inconsistencies in Inventory_Data.csv:

    1. DUPLICATE (site_id, product_id) rows
       → Aggregated: beginning & replenishment summed; ending = max;
         stockout flag = 'Yes' if any row flagged.

    2. Ending inventory != Beginning + Replenishment - Sales (97/100 rows)
       → Recalculated as:
           ending = max(0, beginning + replenishment - net_units_sold)
         where net_units_sold = units_sold - returns per (site_id, product_id).
         Rows with no matching sales record keep their original ending value.

    3. Sites / products in Sales_Data missing from Inventory_Data
       → Auto-created stub rows with estimated stock based on sales volume.
    """
    print("\n  [Inventory Cleaning] Resolving inconsistencies...")

    # Step 1: Merge duplicate (site_id, product_id)
    dups = inv_df.duplicated(['site_id', 'product_id'], keep=False)
    if dups.any():
        dup_pairs = inv_df.loc[dups, ['site_id', 'product_id']].drop_duplicates()
        print(f"  [WARN] {len(dup_pairs)} duplicate (site, product) pairs found - merging:")
        for _, r in dup_pairs.iterrows():
            print(f"         -> ({r['site_id']}, {r['product_id']})")
        agg = {
            'beginning_inventory': 'sum',
            'replenishment':       'sum',
            'ending_inventory':    'max',
            'stockout_flag':       lambda x: 'Yes' if 'Yes' in x.values else 'No'
        }
        inv_df = inv_df.groupby(['site_id', 'product_id'], as_index=False).agg(agg)
        print(f"  [OK] After de-duplication: {len(inv_df)} rows")
    else:
        print("  [OK] No duplicate (site, product) pairs")

    # Step 2: Recalculate ending using actual net sales
    sales_net = (
        sales_df
        .assign(net_sold=sales_df['units_sold'] - sales_df['returns'].fillna(0))
        .groupby(['site_id', 'product_id'], as_index=False)['net_sold']
        .sum()
        .rename(columns={'net_sold': '_net_sold'})
    )
    inv_df = inv_df.merge(sales_net, on=['site_id', 'product_id'], how='left')
    inv_df['_net_sold'] = inv_df['_net_sold'].fillna(0).astype(int)
    recalc = (inv_df['beginning_inventory'] + inv_df['replenishment'] - inv_df['_net_sold']).clip(lower=0)
    changed = (recalc != inv_df['ending_inventory']).sum()
    print(f"  [FIX] Recalculated ending_inventory for {changed} rows "
          f"(beginning + replenishment - net_sales)")
    inv_df['ending_inventory'] = recalc
    inv_df.drop(columns=['_net_sold'], inplace=True)

    # Step 3: Inject stubs for sales combos missing in inventory
    # stubs means dummy / placeholder records that you are creating to fill missing data.

    inv_keys   = set(zip(inv_df['site_id'], inv_df['product_id']))
    sales_keys = set(zip(sales_df['site_id'], sales_df['product_id']))
    missing    = sales_keys - inv_keys
    if missing:
        print(f"  [WARN] {len(missing)} (site, product) combos in Sales lack inventory - adding stubs:")
        stubs = []
        for sid, pid in sorted(missing):
            mask = (sales_df['site_id'] == sid) & (sales_df['product_id'] == pid)
            total_sold    = int(sales_df.loc[mask, 'units_sold'].sum())
            total_returns = int(sales_df.loc[mask, 'returns'].fillna(0).sum())
            net           = max(total_sold - total_returns, 0)
            beg           = max(net * 2, 50)
            rep           = max(net, 0)
            end           = max(beg + rep - net, 0)
            stubs.append({
                'site_id': sid, 'product_id': pid,
                'beginning_inventory': beg,
                'ending_inventory':    end,
                'replenishment':       rep,
                'stockout_flag':       'No'
            })
            print(f"         -> ({sid}, {pid})  begin={beg}  replen={rep}  end={end}")
        inv_df = pd.concat([inv_df, pd.DataFrame(stubs)], ignore_index=True)
        print(f"  [OK] Total inventory rows after stubs: {len(inv_df)}")
    else:
        print("  [OK] All Sales combos present in Inventory")

    return inv_df


# -----------------------------------------------------------------
#  SALES ORDER SEEDING
# -----------------------------------------------------------------
def seed_sales_orders(sales_df, current_user_id, db, SalesOrder, SalesOrderItem, Product):
    """
    Converts Sales_Data rows into SalesOrder + SalesOrderItem records.
    Grouping key: (date, site_id, customer_id) -> one SalesOrder per group.
    Status = 'Shipped' (historical data implies fulfilment).
    total_amount = sum(revenue - discounts).
    """
    print("\n  [SalesOrders] Generating sales orders from Sales Data...")

    date_col     = find_column(sales_df, ['date', 'sale_date'])
    units_col    = find_column(sales_df, ['units_sold', 'units'])
    revenue_col  = find_column(sales_df, ['revenue'])
    discount_col = find_column(sales_df, ['discount', 'discounts'])
    returns_col  = find_column(sales_df, ['return', 'returns'])
    cust_col     = find_column(sales_df, ['customer_id', 'cust_id'])

    so_count   = 0
    item_count = 0
    counter    = 1

    groups = sales_df.groupby(['site_id', cust_col, date_col], dropna=False)

    for (site_id, cust_id, sale_date), grp in groups:
        net_revenue = float(
            grp[revenue_col].sum() - grp[discount_col].fillna(0).sum()
        ) if revenue_col else 0.0

        so = SalesOrder(
            so_number    = f"SO-DATA-{counter:05d}",
            warehouse_id = str(site_id),
            customer_id  = safe_str(cust_id),
            order_date   = safe_date(sale_date),
            status       = 'Delivered',   # historical seed data — fulfilled before app existed
            total_amount = round(net_revenue, 2),
            created_by   = current_user_id
        )
        db.session.add(so)
        db.session.flush()
        counter += 1

        for _, row in grp.iterrows():
            pid     = str(row['product_id'])
            qty     = safe_int(row[units_col]) or 0
            returns = safe_int(row[returns_col]) or 0 if returns_col else 0
            shipped = max(qty - returns, 0)
            rev     = safe_float(row[revenue_col]) or 0.0
            disc    = safe_float(row[discount_col]) or 0.0 if discount_col else 0.0
            unit_p  = round((rev - disc) / qty, 4) if qty else 0.0
            prod    = Product.query.get(pid)

            db.session.add(SalesOrderItem(
                so_id            = so.id,
                product_id       = pid,
                quantity         = qty,
                shipped_quantity = shipped,
                unit_price       = prod.unit_price if prod else unit_p,
                line_total       = round(unit_p * qty, 2)
            ))
            item_count += 1
        so_count += 1

    db.session.commit()
    print(f"  [OK] {so_count} SalesOrders, {item_count} line items created")


# -----------------------------------------------------------------
#  MAIN
# -----------------------------------------------------------------
def run_import():
    from app import create_app
    from app.database import db
    from app.models import (
        State, Manager, Role, User, Product, Site, Customer,
        Inventory, Logistics, Sale, Promotion, SeasonalPlan, StockLevel,
        Category, SubCategory, Supplier, SalesOrder, SalesOrderItem
    )
    from app.auth import hash_password

    app = create_app()
    with app.app_context():
        print("Dropping and recreating schema...")
        db.drop_all()
        db.create_all()
        print("Schema ready.")

        # ── 1. SITE DETAILS ──────────────────────────────────────────────
        print("\n[1/9] Loading Sites...")
        try:
            site_df    = clean_df(pd.read_csv(get_file_path('Site_Details.csv')))
            state_col  = find_column(site_df, ['state', 'province'])
            format_col = find_column(site_df, ['format', 'site_format', 'type'])
            size_col   = find_column(site_df, ['size', 'store_size', 'area'])
            date_col   = find_column(site_df, ['open_date', 'opening', 'opened'])
            region_col = find_column(site_df, ['region'])
            city_col   = find_column(site_df, ['city'])
            status_col = find_column(site_df, ['status'])

            seen_states = {}
            if state_col:
                for s in site_df[state_col].dropna().unique():
                    name = str(s).strip()
                    st = State(state_name=name)
                    db.session.add(st)
                    seen_states[name] = st
            else:
                st = State(state_name="Default State")
                db.session.add(st)
                seen_states["Default State"] = st
            db.session.commit()
            # Refresh to get IDs
            for k in seen_states:
                seen_states[k] = State.query.filter_by(state_name=k).first()

            loaded_site_ids = set()
            for _, row in site_df.iterrows():
                st_name = str(row[state_col]).strip() if state_col else "Default State"
                st_obj  = seen_states.get(st_name)
                db.session.add(Site(
                    site_id    = str(row['site_id']),
                    site_name  = row['site_name'],
                    site_format= safe_str(row[format_col]) if format_col else None,
                    region     = safe_str(row[region_col]) if region_col else None,
                    city       = safe_str(row[city_col]) if city_col else None,
                    state_id   = st_obj.state_id if st_obj else None,
                    store_size = safe_int(row[size_col]) if size_col else None,
                    open_date  = safe_date(row[date_col]) if date_col else None,
                    manager_id = None,
                    status     = safe_str(row[status_col]) if status_col else 'Active'
                ))
                loaded_site_ids.add(str(row['site_id']))
            db.session.commit()

            # Auto-create placeholder sites referenced in Sales
            sales_pre = clean_df(pd.read_csv(get_file_path('Sales_Data.csv')))
            missing_sites = set(sales_pre['site_id'].astype(str).unique()) - loaded_site_ids
            if missing_sites:
                default_state = State.query.first()
                print(f"  [WARN] Sites in Sales missing from Site_Details ({len(missing_sites)}) - adding placeholders:")
                for sid in sorted(missing_sites):
                    print(f"         -> {sid}")
                    db.session.add(Site(
                        site_id  = sid,
                        site_name= f"Auto-created Site {sid}",
                        status   = 'Active',
                        state_id = default_state.state_id if default_state else None
                    ))
                    loaded_site_ids.add(sid)
                db.session.commit()
            print(f"  Sites: {Site.query.count()} total")
        except Exception as e:
            import traceback; print(f"CRITICAL: {e}"); traceback.print_exc(); return

        # ── 2. ROLES + ADMIN ─────────────────────────────────────────────
        print("\n[2/9] Creating Roles & Admin user...")
        admin_role   = Role(role_name='Admin',   description='Administrator')
        manager_role = Role(role_name='Manager', description='Store Manager')
        analyst_role = Role(role_name='Analyst', description='Data Analyst')
        db.session.add_all([admin_role, manager_role, analyst_role])
        db.session.commit()
        admin_user = User(username='admin', email='admin@ifms.com',
                          password_hash=hash_password('Admin@123'),
                          role_id=admin_role.id, state_id='ALL')
        db.session.add(admin_user)
        db.session.commit()
        print("  Admin user created (username: admin, password: Admin@123)")

        # ── 3. SUPPLIERS ─────────────────────────────────────────────────
        print("\n[3/9] Loading Suppliers...")
        try:
            pf = clean_df(pd.read_csv(get_file_path('Product_Information.csv')))
            sc = find_column(pf, ['supplier', 'vendor'])
            if sc:
                for name in pf[sc].dropna().unique():
                    n = str(name).strip()
                    if n and not Supplier.query.filter_by(supplier_name=n).first():
                        db.session.add(Supplier(supplier_name=n, status='Active'))
                db.session.commit()
            print(f"  Suppliers: {Supplier.query.count()}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # ── 4. CATEGORIES ────────────────────────────────────────────────
        print("\n[4/9] Loading Categories & Subcategories...")
        try:
            pc = clean_df(pd.read_csv(get_file_path('Product_Information.csv')))
            cc = find_column(pc, ['category', 'cat'])
            sc2= find_column(pc, ['subcategory', 'sub_category', 'subcat'])
            if cc:
                cols = [cc] + ([sc2] if sc2 else [])
                for _, row in pc[cols].drop_duplicates().iterrows():
                    cat_name = safe_str(row[cc])
                    if not cat_name: continue
                    cat = Category.query.filter_by(category_name=cat_name).first()
                    if not cat:
                        cat = Category(category_name=cat_name, status='Active')
                        db.session.add(cat); db.session.flush()
                    if sc2:
                        sub_name = safe_str(row[sc2])
                        if sub_name and not SubCategory.query.filter_by(subcategory_name=sub_name, category_id=cat.id).first():
                            db.session.add(SubCategory(subcategory_name=sub_name, category_id=cat.id, status='Active'))
                db.session.commit()
            print(f"  Categories: {Category.query.count()}, Subcategories: {SubCategory.query.count()}")
        except Exception as e:
            import traceback; print(f"  ERROR: {e}"); traceback.print_exc()

        # ── 5. PRODUCTS ──────────────────────────────────────────────────
        print("\n[5/9] Loading Products...")
        try:
            prod_df      = clean_df(pd.read_csv(get_file_path('Product_Information.csv')))
            pid_col      = find_column(prod_df, ['product_id', 'prod_id', 'id'])
            pname_col    = find_column(prod_df, ['product_name', 'name', 'item_name'])
            cat_col      = find_column(prod_df, ['category', 'cat'])
            subcat_col   = find_column(prod_df, ['subcategory', 'sub_category', 'subcat'])
            cost_col     = find_column(prod_df, ['unit_cost', 'cost'])
            price_col    = find_column(prod_df, ['unit_price', 'price', 'selling_price'])
            supplier_col = find_column(prod_df, ['supplier', 'vendor'])
            shelf_col    = find_column(prod_df, ['shelf_life', 'shelf', 'expiry'])

            loaded_pids = set()
            for _, row in prod_df.iterrows():
                sup_name = safe_str(row[supplier_col]) if supplier_col else None
                sup_obj  = Supplier.query.filter_by(supplier_name=sup_name).first() if sup_name else None
                db.session.add(Product(
                    product_id  = str(row[pid_col]),
                    product_name= safe_str(row[pname_col]),
                    category    = safe_str(row[cat_col]) if cat_col else None,
                    subcategory = safe_str(row[subcat_col]) if subcat_col else None,
                    unit_cost   = safe_float(row[cost_col]),
                    unit_price  = safe_float(row[price_col]),
                    supplier    = sup_name,
                    supplier_id = sup_obj.supplier_id if sup_obj else None,
                    shelf_life  = safe_int(row[shelf_col]) if shelf_col else None
                ))
                loaded_pids.add(str(row[pid_col]))
            db.session.commit()

            # Auto-create stubs for products in Sales not in Product_Information
            sp = clean_df(pd.read_csv(get_file_path('Sales_Data.csv')))
            missing_pids = set(sp['product_id'].astype(str).unique()) - loaded_pids
            if missing_pids:
                def_sup = Supplier.query.first()
                print(f"  [WARN] {len(missing_pids)} products in Sales missing from Product_Information - creating stubs:")
                for pid in sorted(missing_pids):
                    print(f"         -> {pid}")
                    db.session.add(Product(
                        product_id=pid, product_name=f"Auto-created {pid}",
                        unit_cost=0.0, unit_price=0.0,
                        supplier_id=def_sup.supplier_id if def_sup else None
                    ))
                    loaded_pids.add(pid)
                db.session.commit()
            print(f"  Products: {Product.query.count()} total")
        except Exception as e:
            import traceback; print(f"CRITICAL: {e}"); traceback.print_exc(); return

        # ── 6. CUSTOMERS ─────────────────────────────────────────────────
        print("\n[6/9] Loading Customers...")
        try:
            cust_df    = clean_df(pd.read_csv(get_file_path('Customer_Demographics.csv')))
            cid_col    = find_column(cust_df, ['customer_id', 'cust_id', 'id'])
            age_col    = find_column(cust_df, ['age'])
            gender_col = find_column(cust_df, ['gender', 'sex'])
            income_col = find_column(cust_df, ['income', 'income_bracket', 'bracket'])
            freq_col   = find_column(cust_df, ['frequency', 'purchase_frequency', 'freq'])
            spend_col  = find_column(cust_df, ['spend', 'average_spend', 'avg_spend'])

            loaded_cids = set()
            for _, row in cust_df.iterrows():
                db.session.add(Customer(
                    customer_id       = str(row[cid_col]),
                    age               = safe_int(row[age_col]) if age_col else None,
                    gender            = safe_str(row[gender_col]) if gender_col else None,
                    income_bracket    = safe_str(row[income_col]) if income_col else None,
                    purchase_frequency= safe_int(row[freq_col]) if freq_col else None,
                    average_spend     = safe_float(row[spend_col]) if spend_col else None
                ))
                loaded_cids.add(str(row[cid_col]))
            db.session.commit()

            sp2 = clean_df(pd.read_csv(get_file_path('Sales_Data.csv')))
            cust_col2 = find_column(sp2, ['customer_id', 'cust_id'])
            if cust_col2:
                missing_cids = set(sp2[cust_col2].dropna().astype(str).unique()) - loaded_cids
                if missing_cids:
                    for cid in sorted(missing_cids):
                        db.session.add(Customer(customer_id=cid))
                    db.session.commit()
                    print(f"  [WARN] {len(missing_cids)} customer stubs added from Sales")
            print(f"  Customers: {Customer.query.count()} total")
        except Exception as e:
            import traceback; print(f"CRITICAL: {e}"); traceback.print_exc(); return

        # ── 7. INVENTORY (inconsistency-aware) ───────────────────────────
        print("\n[7/9] Loading Inventory (with inconsistency resolution)...")
        try:
            inv_raw   = clean_df(pd.read_csv(get_file_path('Inventory_Data.csv')))
            sales_raw = clean_df(pd.read_csv(get_file_path('Sales_Data.csv')))

            # Normalise inventory column names
            bc = find_column(inv_raw, ['beginning', 'begin_inventory', 'opening'])
            ec = find_column(inv_raw, ['ending', 'end_inventory', 'closing'])
            rc = find_column(inv_raw, ['replenishment', 'replen', 'restock'])
            fc = find_column(inv_raw, ['stockout', 'stock_out', 'out_of_stock'])
            rmap = {}
            if bc and bc != 'beginning_inventory': rmap[bc] = 'beginning_inventory'
            if ec and ec != 'ending_inventory':    rmap[ec] = 'ending_inventory'
            if rc and rc != 'replenishment':        rmap[rc] = 'replenishment'
            if fc and fc != 'stockout_flag':        rmap[fc] = 'stockout_flag'
            if rmap: inv_raw.rename(columns=rmap, inplace=True)

            # Normalise sales column names
            uc = find_column(sales_raw, ['units_sold', 'units'])
            ret_c = find_column(sales_raw, ['return', 'returns'])
            if uc and uc != 'units_sold':   sales_raw.rename(columns={uc: 'units_sold'}, inplace=True)
            if ret_c and ret_c != 'returns': sales_raw.rename(columns={ret_c: 'returns'}, inplace=True)

            inv_clean = clean_inventory_data(inv_raw, sales_raw)

            for _, row in inv_clean.iterrows():
                beg  = safe_int(row.get('beginning_inventory'))
                end  = safe_int(row.get('ending_inventory'))
                rep  = safe_int(row.get('replenishment'))
                flag = safe_str(row.get('stockout_flag'))
                sid  = str(row['site_id'])
                pid  = str(row['product_id'])

                db.session.add(Inventory(
                    site_id             = sid,
                    product_id          = pid,
                    beginning_inventory = beg,
                    ending_inventory    = end,
                    replenishment       = rep,
                    stockout_flag       = flag
                ))

                # Sync StockLevel (current stock = ending inventory)
                sl = StockLevel.query.filter_by(product_id=pid, site_id=sid).first()
                if sl:
                    sl.current_quantity = end or 0
                else:
                    db.session.add(StockLevel(
                        product_id=pid, site_id=sid,
                        current_quantity=end or 0
                    ))

            db.session.commit()
            print(f"  Inventory: {Inventory.query.count()} rows | StockLevels: {StockLevel.query.count()}")
        except Exception as e:
            import traceback; print(f"CRITICAL: {e}"); traceback.print_exc(); return

        # ── 8. SALES FACTS ───────────────────────────────────────────────
        print("\n[8/9] Loading Sales fact rows...")
        try:
            sales_df = clean_df(pd.read_csv(get_file_path('Sales_Data.csv')))
            d_col  = find_column(sales_df, ['date', 'sale_date', 'transaction_date'])
            u_col  = find_column(sales_df, ['units_sold', 'units', 'quantity_sold'])
            rv_col = find_column(sales_df, ['revenue', 'total_revenue', 'sales'])
            dc_col = find_column(sales_df, ['discount', 'discounts'])
            rt_col = find_column(sales_df, ['return', 'returns'])
            cu_col = find_column(sales_df, ['customer_id', 'cust_id', 'customer'])

            for _, row in sales_df.iterrows():
                db.session.add(Sale(
                    date       = safe_date(row[d_col]),
                    site_id    = str(row['site_id']),
                    product_id = str(row['product_id']),
                    units_sold = safe_int(row[u_col]),
                    revenue    = safe_float(row[rv_col]),
                    discounts  = safe_float(row[dc_col]) if dc_col else None,
                    returns    = safe_int(row[rt_col]) if rt_col else None,
                    customer_id= safe_str(row[cu_col]) if cu_col else None
                ))
            db.session.commit()
            print(f"  Sales facts: {Sale.query.count()} rows")

            # --- Sales Orders (from same data) ---
            seed_sales_orders(
                sales_df=sales_df,
                current_user_id=admin_user.id,
                db=db, SalesOrder=SalesOrder,
                SalesOrderItem=SalesOrderItem,
                Product=Product
            )
        except Exception as e:
            import traceback; print(f"CRITICAL: {e}"); traceback.print_exc(); return

        # ── 9. OPTIONAL: Logistics, Promotions, Seasonal Plans ──────────
        print("\n[9/9] Loading optional datasets...")

        if os.path.exists(get_file_path('Logistics_Data.csv')):
            try:
                ldf = clean_df(pd.read_csv(get_file_path('Logistics_Data.csv')))
                ldc = find_column(ldf, ['date', 'shipment_date', 'ship_date'])
                lsc = find_column(ldf, ['status', 'delivery_status', 'delivery'])
                ltc = find_column(ldf, ['transport', 'transportation', 'mode'])

                # Ensure all product_ids referenced in logistics exist in products table
                # to avoid FK violation (psycopg2.errors.ForeignKeyViolation)
                existing_pids = set(
                    pid for (pid,) in db.session.query(Product.product_id).all()
                )
                logistics_pids = set(ldf['product_id'].astype(str).unique())
                missing_pids = logistics_pids - existing_pids
                if missing_pids:
                    def_sup = Supplier.query.first()
                    print(f"  [WARN] {len(missing_pids)} product(s) in Logistics missing from products table - creating stubs:")
                    for pid in sorted(missing_pids):
                        print(f"         -> {pid}")
                        db.session.add(Product(
                            product_id=pid, product_name=f"Auto-created {pid}",
                            unit_cost=0.0, unit_price=0.0,
                            supplier_id=def_sup.supplier_id if def_sup else None
                        ))
                    db.session.commit()

                for _, row in ldf.iterrows():
                    db.session.add(Logistics(
                        shipment_id=str(row['shipment_id']), site_id=str(row['site_id']),
                        product_id=str(row['product_id']), quantity=safe_int(row['quantity']),
                        shipment_date=safe_date(row[ldc]) if ldc else None,
                        delivery_status=safe_str(row[lsc]) if lsc else None,
                        transportation_type=safe_str(row[ltc]) if ltc else None
                    ))
                db.session.commit()
                print(f"  Logistics: {len(ldf)} rows")
            except Exception as e:
                db.session.rollback()
                print(f"  Logistics error: {e}")

        if os.path.exists(get_file_path('Promotions_and_Discounts.csv')):
            try:
                pdf = clean_df(pd.read_csv(get_file_path('Promotions_and_Discounts.csv')))
                ps = find_column(pdf, ['start', 'start_date'])
                pe = find_column(pdf, ['end', 'end_date'])
                pt = find_column(pdf, ['discount_type', 'type', 'promo_type'])
                pa = find_column(pdf, ['discount_amount', 'amount', 'value'])

                # Ensure all product_ids referenced in promotions exist in products table
                existing_pids = set(
                    pid for (pid,) in db.session.query(Product.product_id).all()
                )
                promo_pids = set(pdf['product_id'].astype(str).unique())
                missing_pids = promo_pids - existing_pids
                if missing_pids:
                    def_sup = Supplier.query.first()
                    print(f"  [WARN] {len(missing_pids)} product(s) in Promotions missing from products table - creating stubs:")
                    for pid in sorted(missing_pids):
                        print(f"         -> {pid}")
                        db.session.add(Product(
                            product_id=pid, product_name=f"Auto-created {pid}",
                            unit_cost=0.0, unit_price=0.0,
                            supplier_id=def_sup.supplier_id if def_sup else None
                        ))
                    db.session.commit()

                for _, row in pdf.iterrows():
                    db.session.add(Promotion(
                        promotion_id=str(row['promotion_id']), product_id=str(row['product_id']),
                        site_id=str(row['site_id']),
                        start_date=safe_date(row[ps]) if ps else None,
                        end_date=safe_date(row[pe]) if pe else None,
                        discount_type=safe_str(row[pt]) if pt else None,
                        discount_amount=safe_float(row[pa]) if pa else None
                    ))
                db.session.commit()
                print(f"  Promotions: {len(pdf)} rows")
            except Exception as e:
                db.session.rollback()
                print(f"  Promotions error: {e}")

        if os.path.exists(get_file_path('Monthly_Seasonal_Planning.csv')):
            try:
                spdf = clean_df(pd.read_csv(get_file_path('Monthly_Seasonal_Planning.csv')))
                fc2  = find_column(spdf, ['forecast', 'forecasted_sales', 'planned'])
                ac2  = find_column(spdf, ['actual', 'actual_sales'])
                sac  = find_column(spdf, ['seasonal', 'adjustment', 'seasonal_adjustments'])
                for _, row in spdf.iterrows():
                    db.session.add(SeasonalPlan(
                        month=row['month'], site_id=str(row['site_id']),
                        product_category=row['product_category'],
                        forecasted_sales=safe_float(row[fc2]) if fc2 else None,
                        actual_sales=safe_float(row[ac2]) if ac2 else None,
                        seasonal_adjustments=safe_float(row[sac]) if sac else None
                    ))
                db.session.commit()
                print(f"  Seasonal plans: {len(spdf)} rows")
            except Exception as e:
                print(f"  Seasonal plans error: {e}")

        # ── Summary ──────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("  ALL DATA SEEDED SUCCESSFULLY")
        print("=" * 60)
        print(f"  Sites          : {Site.query.count()}")
        print(f"  Products       : {Product.query.count()}")
        print(f"  Customers      : {Customer.query.count()}")
        print(f"  Inventory rows : {Inventory.query.count()}")
        print(f"  Stock Levels   : {StockLevel.query.count()}")
        print(f"  Sales (facts)  : {Sale.query.count()}")
        print(f"  Sales Orders   : {SalesOrder.query.count()}")
        print(f"  SO Line Items  : {SalesOrderItem.query.count()}")
        print("=" * 60)


if __name__ == "__main__":
    run_import()
