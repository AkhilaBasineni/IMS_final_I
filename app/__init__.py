from flask import Flask, render_template
from app.database import db, migrate, jwt, mail
from app.config import Config

def create_app():
    app = Flask(__name__, template_folder='templates', static_folder='../static')
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    mail.init_app(app)

    with app.app_context():
        from app.routers import (auth_routes, product_routes, user_routes, warehouse_routes,
                                 stock_routes, purchase_order_routes, sales_order_routes,
                                 report_routes, analytics_routes, audit_routes, states_routes,
                                 inventory_routes, customer_routes, logistics_routes,
                                 contact_routes, promotions_routes, chatbot_routes,
                                 so_return_routes, seasonal_routes,
                                 po_return_routes, supplier_routes)

        app.register_blueprint(auth_routes.bp)
        app.register_blueprint(product_routes.bp)
        app.register_blueprint(user_routes.bp)
        app.register_blueprint(warehouse_routes.bp)
        app.register_blueprint(stock_routes.bp)
        app.register_blueprint(purchase_order_routes.bp)
        app.register_blueprint(sales_order_routes.bp)
        app.register_blueprint(report_routes.bp)
        app.register_blueprint(analytics_routes.bp)
        app.register_blueprint(audit_routes.bp)
        app.register_blueprint(states_routes.bp)
        app.register_blueprint(inventory_routes.bp)
        app.register_blueprint(customer_routes.bp)
        app.register_blueprint(logistics_routes.bp)
        app.register_blueprint(contact_routes.bp)
        app.register_blueprint(promotions_routes.bp)
        app.register_blueprint(chatbot_routes.bp)
        app.register_blueprint(so_return_routes.bp_so_returns)
        app.register_blueprint(seasonal_routes.bp)
        app.register_blueprint(po_return_routes.bp_po_returns)
        app.register_blueprint(supplier_routes.bp_suppliers)

   

    # PAGE ROUTES
    @app.route('/')
    def index(): 
        return render_template('index.html') # New Public Home Page

    @app.route('/about')
    def about(): 
        return render_template('about.html') # New About Page

    @app.route('/contact')
    def contact(): 
        return render_template('contact.html') # New Contact Page

    @app.route('/contact-messages')
    def contact_messages_page():
        return render_template('contact_messages.html')  # Admin contact inbox

    @app.route('/login')
    def login(): 
        return render_template('login.html')

    @app.route('/admin-dashboard')
    def admin_dashboard(): return render_template('admin_dashboard.html')

    @app.route('/manager-dashboard')
    def manager_dashboard(): return render_template('manager_dashboard.html')

    @app.route('/analyst-dashboard')
    def analyst_dashboard(): return render_template('analyst_dashboard.html')

    @app.route('/products')
    def products_page(): return render_template('products.html')

    @app.route('/warehouses')
    def warehouses_page(): return render_template('warehouses.html')

    @app.route('/stock-levels')
    def stock_levels_page(): return render_template('stock_levels.html')

    @app.route('/users')
    def users_page(): return render_template('users.html')

    @app.route('/purchase-orders')
    def po_page(): return render_template('purchase_orders.html')

    @app.route('/sales-orders')
    def so_page(): return render_template('sales_orders.html')

    @app.route('/reports')
    def reports_page(): return render_template('reports.html')

    @app.route('/audit-logs')
    def audit_logs_page(): return render_template('audit_logs.html')

    @app.route('/inventory')
    def inventory_page(): return render_template('inventory.html')

    @app.route('/customers')
    def customers_page(): return render_template('customers.html')

    @app.route('/logistics')
    def logistics_page(): return render_template('logistics.html')

    @app.route('/promotions')
    def promotions_page(): return render_template('promotions.html')

    @app.route('/promotions-view')
    def promotions_view_page(): return render_template('promotions_view.html')

    @app.route('/sales-order-returns')
    def so_returns_page(): return render_template('sales_order_returns.html')

    @app.route('/purchase-order-returns')
    def po_returns_page(): return render_template('purchase_order_returns.html')

    @app.route('/suppliers')
    def suppliers_page(): return render_template('suppliers.html')

    @app.route('/seasonal-planning')
    def seasonal_planning_page(): return render_template('seasonal_planning.html')

    return app