from flask import Flask, render_template, request, redirect, jsonify, send_file, session, flash, url_for
import sqlite3
from datetime import datetime, timedelta
import uuid
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import math
import os
from functools import wraps
import json
from io import BytesIO
import base64

app = Flask(__name__)
# Use an environment variable for the secret key for security
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-for-local-testing')

# =====================================================
# CONFIGURATION
# =====================================================

PRODUCT_RATES = {
    # Chain Link
    ("Chain Link", "3ft / 12 Gauge"): 80,
    ("Chain Link", "4ft / 12 Gauge"): 95,
    ("Chain Link", "4ft / 10 Gauge"): 110,
    ("Chain Link", "5ft / 10 Gauge"): 135,
    ("Chain Link", "6ft / 10 Gauge"): 165,
    ("Chain Link", "6ft / 8 Gauge"): 180,
    ("Chain Link", "8ft / 8 Gauge"): 195,
    ("Chain Link", "10ft / 8 Gauge"): 225,
    # Barbed Wire
    ("Barbed Wire", "12x12 Gauge"): 65,
    ("Barbed Wire", "12x14 Gauge"): 70,
    ("Barbed Wire", "14x14 Gauge"): 75,
}

# User roles and permissions (loaded from environment variable as JSON)
# IMPORTANT: Store user data securely, not hardcoded.
try:
    USERS = json.loads(os.environ.get('APP_USERS', '{}'))
    if not USERS:
        print("⚠️ WARNING: APP_USERS environment variable not set. Using default insecure user.")
        USERS = {"admin": {"password": "admin123", "role": "owner", "name": "Admin"}}
except json.JSONDecodeError:
    print("⚠️ ERROR: Could not parse APP_USERS. Using default insecure user.")
    USERS = {"admin": {"password": "admin123", "role": "owner", "name": "Admin"}}

# Email configuration from environment variables
# IMPORTANT: Never hardcode passwords in your code.
EMAIL_CONFIG = {
    "SMTP_SERVER": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
    "SMTP_PORT": int(os.environ.get("SMTP_PORT", 465)),
    "SENDER_EMAIL": os.environ.get("SENDER_EMAIL"),
    "SENDER_PASSWORD": os.environ.get("SENDER_PASSWORD")
}

RECIPIENT_EMAILS = [email.strip() for email in os.environ.get("RECIPIENT_EMAILS", "").split(',') if email.strip()]

# Define separate workflows for different order types
WORKFLOW_STEPS = {
    "Material Purchase": [
        "Order placed",
        "Processing",
        "Ready for dispatch",
        "In transit",
        "Delivered/installed",
        "Settled",
        "Closed",
        "Cancelled"
    ],
    "Fencing Contract Job": [
        "Order placed",
        "Site Survey",
        "Estimation Approved",
        "Material Dispatched",
        "Installation Started",
        "Installation Complete",
        "Handover Done",
        "Settled",
        "Closed",
        "Cancelled"
    ]
}

# =====================================================
# DATABASE INITIALIZATION
# =====================================================

def init_db():
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()

        # Orders table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            customer_id TEXT,
            name TEXT,
            mobile TEXT,
            country TEXT,
            state TEXT,
            city TEXT,
            pincode TEXT,
            address TEXT,
            order_type TEXT,

            acres REAL,
            no_of_units REAL,
            soil_type TEXT,
            distance_km REAL,

            product_type TEXT,
            product_material TEXT,
            dimension TEXT,

            material_cost REAL,
            installation_cost REAL,
            transport_cost REAL,
            total_cost REAL,
            advance_payment REAL,
            advance_paid REAL,
            balance_due REAL,

            delivery_date TEXT,
            payment_status TEXT DEFAULT 'Unpaid',
            status TEXT DEFAULT 'Order placed',
            priority INTEGER DEFAULT 3,
            assigned_to TEXT,
            created_at TEXT,
            created_by TEXT,
            updated_at TEXT,
            notes TEXT
        )
        """)

        # Add payment_status column if it doesn't exist (for existing databases)
        cursor.execute("PRAGMA table_info(orders)")
        if 'payment_status' not in [col[1] for col in cursor.fetchall()]:
            cursor.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'Unpaid'")

        # Customers table for better tracking
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            name TEXT,
            mobile TEXT UNIQUE,
            email TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            pincode TEXT,
            country TEXT,
            total_orders INTEGER DEFAULT 0,
            total_spent REAL DEFAULT 0,
            created_at TEXT,
            last_order_date TEXT
        )
        """)

        # Payments table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            order_id TEXT,
            customer_id TEXT,
            amount REAL,
            payment_date TEXT,
            payment_method TEXT,
            reference_number TEXT,
            notes TEXT,
            created_at TEXT,
            FOREIGN KEY(order_id) REFERENCES orders(order_id)
        )
        """)

        # Activity log
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            action TEXT,
            entity_type TEXT,
            entity_id TEXT,
            details TEXT,
            timestamp TEXT
        )
        """)

        conn.commit()
    finally:
        conn.close()

init_db()

# =====================================================
# AUTHENTICATION & AUTHORIZATION
# =====================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash('Please login to access this page', 'warning')
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated_function

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                return redirect('/login')
            if session.get('role') not in roles:
                flash('You do not have permission to access this page', 'danger')
                return redirect('/dashboard')
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# =====================================================
# HELPER FUNCTIONS
# =====================================================

def log_activity(user, action, entity_type, entity_id, details=""):
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO activity_log (user, action, entity_type, entity_id, details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user, action, entity_type, entity_id, details, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()

def get_or_create_customer(mobile, name, address, city, state, pincode, country):
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        
        # Check if customer exists
        cursor.execute("SELECT customer_id FROM customers WHERE mobile = ?", (mobile,))
        existing = cursor.fetchone()
        
        if existing:
            customer_id = existing[0]
            # Update customer info
            cursor.execute("""
                UPDATE customers SET name=?, address=?, city=?, state=?, pincode=?, country=?
                WHERE customer_id=?
            """, (name, address, city, state, pincode, country, customer_id))
        else:
            # Create new customer
            customer_id = "CUST-" + uuid.uuid4().hex[:8].upper()
            cursor.execute("""
                INSERT INTO customers (customer_id, name, mobile, address, city, state, pincode, country, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (customer_id, name, mobile, address, city, state, pincode, country, 
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        
        conn.commit()
        return customer_id
    finally:
        conn.close()

def update_customer_stats(customer_id):
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE customers SET 
                total_orders = (SELECT COUNT(*) FROM orders WHERE customer_id = ?),
                total_spent = (SELECT COALESCE(SUM(total_cost), 0) FROM orders WHERE customer_id = ?),
                last_order_date = (SELECT MAX(created_at) FROM orders WHERE customer_id = ?)
            WHERE customer_id = ?
        """, (customer_id, customer_id, customer_id, customer_id))
        conn.commit()
    finally:
        conn.close()

def calculate_cost(acres, product_type, dimension, order_type, soil_type=None, no_of_units=0):
    perimeter = 0
    if acres and float(acres) > 0:
        area_sqft = float(acres) * 43560
        side = math.sqrt(area_sqft)
        perimeter = 4 * side
        perimeter = perimeter * 1.05  # 5% wastage
    elif no_of_units and float(no_of_units) > 0:
        perimeter = float(no_of_units)

    rate = PRODUCT_RATES.get((product_type, dimension), 120)
    material_cost = perimeter * rate
    installation_cost = 0
    transport_cost = 0

    if order_type == "Fencing Contract Job":
        soil_multiplier = {"Normal": 1.0, "Rocky": 1.4, "Clay": 1.2}
        multiplier = soil_multiplier.get(soil_type, 1.0)
        installation_cost = material_cost * 0.25 * multiplier

    total_cost = material_cost + installation_cost + transport_cost

    if order_type == "Material Purchase":
        advance = total_cost * 0.40
    else:
        advance = total_cost * 0.55

    return (
        round(material_cost, 2), round(installation_cost, 2),
        round(transport_cost, 2), round(total_cost, 2),
        round(advance, 2)
    )

def send_email_notification(order_data):
    """Enhanced email notification with better formatting"""
    sender_email = EMAIL_CONFIG["SENDER_EMAIL"]
    password = EMAIL_CONFIG["SENDER_PASSWORD"]

    if not sender_email or not password:
        print("[EMAIL] Skipping - please configure email settings in environment variables")
        return False

    if not RECIPIENT_EMAILS:
        print("[EMAIL] Skipping - No recipient emails configured in RECIPIENT_EMAILS")
        return False

    print(f"[EMAIL DEBUG] Attempting to send email from {sender_email} to {RECIPIENT_EMAILS}")
    
    subject = f"🏭 New Order #{order_data['order_id']} - {order_data['product_type']}"
    
    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="background-color: #0066FF; color: white; padding: 20px; text-align: center;">
            <h1>New Factory Order Received</h1>
        </div>
        <div style="padding: 20px;">
            <h2>Order Details</h2>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Order ID:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">{order_data['order_id']}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Customer:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">{order_data.get('name', 'N/A')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Mobile:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">{order_data.get('mobile', 'N/A')}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Product:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">{order_data['product_type']} - {order_data['dimension']}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Material:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">{order_data['product_material']}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Total Cost:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>₹{order_data['total_cost']:.2f}</strong></td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Advance Required:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">₹{order_data['advance_payment']:.2f}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Delivery Date:</strong></td><td style="padding: 8px; border-bottom: 1px solid #ddd;">{order_data['delivery_date']}</td></tr>
            </table>
            <p style="margin-top: 20px; padding: 15px; background-color: #f0f0f0; border-left: 4px solid #0066FF;">
                <strong>Action Required:</strong> Please review and assign this order to the appropriate team.
            </p>
        </div>
    </body>
    </html>
    """
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = ", ".join(RECIPIENT_EMAILS)
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP_SSL(EMAIL_CONFIG["SMTP_SERVER"], EMAIL_CONFIG["SMTP_PORT"]) as server:
            server.login(sender_email, password)
            server.sendmail(sender_email, RECIPIENT_EMAILS, msg.as_string())
            print(f"[EMAIL] Sent successfully to {', '.join(RECIPIENT_EMAILS)}")
            return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

# =====================================================
# AUTHENTICATION ROUTES
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if username in USERS and USERS[username]["password"] == password:
            session['user'] = username
            session['role'] = USERS[username]["role"]
            session['name'] = USERS[username]["name"]
            log_activity(username, "LOGIN", "system", "login", "User logged in")
            flash(f'Welcome back, {USERS[username]["name"]}!', 'success')
            return redirect('/dashboard')
        else:
            flash('Invalid credentials', 'danger')
    
    return render_template("login.html")

@app.route("/logout")
def logout():
    user = session.get('user', 'Unknown')
    log_activity(user, "LOGOUT", "system", "logout", "User logged out")
    session.clear()
    flash('You have been logged out', 'info')
    return redirect('/login')

# =====================================================
# DASHBOARD & ANALYTICS
# =====================================================

@app.route("/")
def index():
    if 'user' in session:
        return redirect('/dashboard')
    return redirect('/login')

@app.route("/dashboard")
@login_required
def dashboard():
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get summary stats
        cursor.execute("SELECT COUNT(*) as total FROM orders")
        total_orders = cursor.fetchone()['total']
        
        cursor.execute("SELECT COUNT(*) as total FROM customers")
        total_customers = cursor.fetchone()['total']
        
        cursor.execute("SELECT COALESCE(SUM(total_cost), 0) as total FROM orders WHERE status != 'Cancelled'")
        total_revenue = cursor.fetchone()['total']
        
        cursor.execute("SELECT COALESCE(SUM(advance_paid), 0) as total FROM orders")
        total_collected = cursor.fetchone()['total']
        
        # Recent orders
        cursor.execute("""
            SELECT * FROM orders 
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        recent_orders = cursor.fetchall()
        
        # Orders by status
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM orders 
            GROUP BY status
        """)
        orders_by_status = cursor.fetchall()
        
        # Monthly revenue (last 6 months)
        cursor.execute("""
            SELECT strftime('%Y-%m', created_at) as month, 
                   COUNT(*) as orders,
                   SUM(total_cost) as revenue
            FROM orders
            WHERE created_at >= date('now', '-6 months')
            GROUP BY month
            ORDER BY month
        """)
        monthly_data = cursor.fetchall()
        
        return render_template("dashboard.html",
                             total_orders=total_orders,
                             total_customers=total_customers,
                             total_revenue=total_revenue,
                             total_collected=total_collected,
                             recent_orders=recent_orders,
                             orders_by_status=orders_by_status,
                             monthly_data=monthly_data)
    finally:
        conn.close()

# =====================================================
# ORDER ROUTES
# =====================================================

@app.route("/new_order")
@login_required
def new_order():
    return render_template("order_form.html", order=None)

@app.route("/submit_order", methods=["POST"])
@login_required
def submit_order():
    try:
        # Extract form data
        name = request.form["name"]
        mobile = request.form["mobile"]
        country = request.form.get("country", "India")
        state = request.form["state"]
        city = request.form["city"]
        pincode = request.form["pincode"]
        address = request.form["address"]
        product_type = request.form["product_type"]
        product_material = request.form["product_material"]
        dimension = request.form["dimension"]
        order_type = request.form["order_type"]
        delivery_date = request.form["delivery_date"]
        notes = request.form.get("notes", "")
        
        acres = request.form.get("acres", 0)
        if not acres or acres == "": acres = 0
        no_of_units = request.form.get("no_of_units", 0)
        if not no_of_units or no_of_units == "": no_of_units = 0
        soil_type = request.form.get("soil_type", "Normal")

        # Get or create customer
        customer_id = get_or_create_customer(mobile, name, address, city, state, pincode, country)
        order_id = "ORD-" + uuid.uuid4().hex[:8].upper()

        # Calculate costs
        material_cost, installation_cost, transport_cost, total_cost, advance_payment = calculate_cost(
            acres, product_type, dimension, order_type, soil_type, no_of_units
        )

        # Insert order
        conn = sqlite3.connect("factory.db")
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO orders
                (order_id, customer_id, name, mobile, country, state, city, pincode, address, 
                 acres, no_of_units, product_type, product_material, dimension, order_type,
                 soil_type, material_cost, installation_cost, transport_cost, total_cost, advance_payment, 
                 advance_paid, balance_due, delivery_date, payment_status, status, created_at, created_by, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id, customer_id, name, mobile, country, state, city, pincode, address,
                acres, no_of_units, product_type, product_material, dimension, order_type,
                soil_type, material_cost, installation_cost, transport_cost, total_cost,
                advance_payment, 0, total_cost, delivery_date, 'Unpaid', 'Order placed',
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session.get('user', 'system'),
                notes
            ))
            conn.commit()
        finally:
            conn.close()

        # Update customer stats
        update_customer_stats(customer_id)

        # Log activity
        log_activity(session.get('user'), "CREATE", "order", order_id, f"New order created for {name}")

        # Send notifications
        order_info = {
            "order_id": order_id,
            "customer_id": customer_id,
            "name": name,
            "mobile": mobile,
            "product_type": product_type,
            "product_material": product_material,
            "dimension": dimension,
            "acres": acres,
            "no_of_units": no_of_units,
            "total_cost": total_cost,
            "advance_payment": advance_payment,
            "delivery_date": delivery_date
        }
        if send_email_notification(order_info):
            flash(f'Order {order_id} created successfully! Email sent.', 'success')
        else:
            flash(f'Order {order_id} created, but email failed. Check server logs.', 'warning')
            
        return redirect(f'/order_details/{order_id}')
    
    except Exception as e:
        flash(f'Error creating order: {str(e)}', 'danger')
        return redirect('/new_order')

@app.route("/orders")
@login_required
def orders():
    # Get filter parameters
    status_filter = request.args.get('status', '')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM orders WHERE 1=1"
        params = []
        
        if status_filter:
            query += " AND status = ?"
            params.append(status_filter)
        
        if search:
            query += " AND (name LIKE ? OR mobile LIKE ? OR order_id LIKE ?)"
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])
        
        if date_from:
            query += " AND date(created_at) >= ?"
            params.append(date_from)
        
        if date_to:
            query += " AND date(created_at) <= ?"
            params.append(date_to)
        
        query += " ORDER BY created_at DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Get unique statuses for filter dropdown
        cursor.execute("SELECT DISTINCT status FROM orders ORDER BY status")
        statuses = [row['status'] for row in cursor.fetchall()]
        
    finally:
        conn.close()

    return render_template("orders_list.html", rows=rows, statuses=statuses,
                         status_filter=status_filter, search=search,
                         date_from=date_from, date_to=date_to,
                         workflows=WORKFLOW_STEPS)

@app.route("/order_details/<order_id>")
@login_required
def order_details(order_id):
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found', 'warning')
            return redirect('/orders')
        
        # Get payment history
        cursor.execute("""
            SELECT * FROM payments 
            WHERE order_id = ? 
            ORDER BY payment_date DESC
        """, (order_id,))
        payments = cursor.fetchall()
        
        # Get activity log
        cursor.execute("""
            SELECT * FROM activity_log 
            WHERE entity_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 20
        """, (order_id,))
        activities = cursor.fetchall()

        current_workflow = WORKFLOW_STEPS.get(order['order_type'], WORKFLOW_STEPS["Material Purchase"])
        
        return render_template("order_details.html", 
                             order=order, 
                             payments=payments,
                             activities=activities,
                             workflow_steps=current_workflow)
    finally:
        conn.close()

@app.route("/edit_order/<order_id>")
@login_required
def edit_order(order_id):
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        order = cursor.fetchone()
        
        if not order:
            flash('Order not found', 'warning')
            return redirect('/orders')
        
        current_workflow = WORKFLOW_STEPS.get(order['order_type'], WORKFLOW_STEPS["Material Purchase"])
        
        return render_template("order_form.html", order=order, workflow_steps=current_workflow)
    finally:
        conn.close()

@app.route("/update_order", methods=["POST"])
@login_required
def update_order():
    order_id = request.form["order_id"]
    
    try:
        name = request.form["name"]
        mobile = request.form["mobile"]
        country = request.form.get("country", "India")
        state = request.form["state"]
        city = request.form["city"]
        pincode = request.form["pincode"]
        address = request.form["address"]
        product_type = request.form["product_type"]
        product_material = request.form["product_material"]
        dimension = request.form["dimension"]
        order_type = request.form["order_type"]
        delivery_date = request.form["delivery_date"]
        status = request.form["status"]
        notes = request.form.get("notes", "")
        
        acres = request.form.get("acres", 0)
        if not acres or acres == "": acres = 0
        no_of_units = request.form.get("no_of_units", 0)
        if not no_of_units or no_of_units == "": no_of_units = 0
        soil_type = request.form.get("soil_type", "Normal")

        # Recalculate costs
        material_cost, installation_cost, transport_cost, total_cost, advance_payment = calculate_cost(
            acres, product_type, dimension, order_type, soil_type, no_of_units
        )

        conn = sqlite3.connect("factory.db")
        try:
            cursor = conn.cursor()
            
            # Get current advance_paid
            cursor.execute("SELECT advance_paid FROM orders WHERE order_id = ?", (order_id,))
            current_paid = cursor.fetchone()[0]
            
            balance_due = total_cost - current_paid

            cursor.execute("""
                UPDATE orders SET
                name=?, mobile=?, country=?, state=?, city=?, pincode=?, address=?, 
                acres=?, no_of_units=?, product_type=?, product_material=?, dimension=?, order_type=?,
                soil_type=?, material_cost=?, installation_cost=?, transport_cost=?, total_cost=?, 
                advance_payment=?, balance_due=?, delivery_date=?, status=?, notes=?,
                updated_at=?
                WHERE order_id=?
            """, (name, mobile, country, state, city, pincode, address, acres, no_of_units, 
                  product_type, product_material, dimension, order_type, soil_type, 
                  material_cost, installation_cost, transport_cost, total_cost, advance_payment, 
                  balance_due, delivery_date, status, notes,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  order_id))

            conn.commit()
        finally:
            conn.close()

        log_activity(session.get('user'), "UPDATE", "order", order_id, f"Order updated")
        flash('Order updated successfully!', 'success')
        return redirect(f'/order_details/{order_id}')
    
    except Exception as e:
        flash(f'Error updating order: {str(e)}', 'danger')
        return redirect(f'/edit_order/{order_id}')

@app.route("/delete_order", methods=["POST"])
@login_required
@role_required('owner', 'manager')
def delete_order():
    order_id = request.form["order_id"]
    
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
        cursor.execute("DELETE FROM payments WHERE order_id = ?", (order_id,))
        conn.commit()
    finally:
        conn.close()
    
    log_activity(session.get('user'), "DELETE", "order", order_id, "Order deleted")
    flash('Order deleted successfully', 'success')
    return redirect("/orders")

@app.route("/update_status", methods=["POST"])
@login_required
def update_status():
    order_id = request.form["order_id"]
    new_status = request.form["status"]
    
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", 
                      (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id))
        conn.commit()
    finally:
        conn.close()
    
    log_activity(session.get('user'), "UPDATE_STATUS", "order", order_id, f"Status changed to: {new_status}")
    flash(f'Order status updated to: {new_status}', 'success')
    
    if request.form.get('redirect') == 'orders':
        return redirect('/orders')
    return redirect(f'/order_details/{order_id}')

# =====================================================
# PAYMENT ROUTES
# =====================================================

@app.route("/add_payment/<order_id>", methods=["POST"])
@login_required
def add_payment(order_id):
    amount = float(request.form["amount"])
    payment_method = request.form["payment_method"]
    reference = request.form.get("reference", "")
    payment_notes = request.form.get("payment_notes", "")
    
    payment_id = "PAY-" + uuid.uuid4().hex[:8].upper()
    
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        
        # Get customer_id
        cursor.execute("SELECT customer_id, advance_paid FROM orders WHERE order_id = ?", (order_id,))
        order_data = cursor.fetchone()
        customer_id = order_data[0]
        current_paid = order_data[1]
        
        # Insert payment
        cursor.execute("""
            INSERT INTO payments 
            (payment_id, order_id, customer_id, amount, payment_date, payment_method, 
             reference_number, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (payment_id, order_id, customer_id, amount, 
              datetime.now().strftime("%Y-%m-%d"), payment_method, reference, payment_notes,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        
        # Update order advance_paid and balance
        new_paid = current_paid + amount
        cursor.execute("SELECT total_cost FROM orders WHERE order_id = ?", (order_id,))
        total_cost = cursor.fetchone()[0]
        
        new_balance = total_cost - new_paid
        payment_status = 'Partially Paid'
        if new_balance <= 0:
            payment_status = 'Paid'

        cursor.execute("""
            UPDATE orders 
            SET advance_paid = ?,
                balance_due = ?,
                payment_status = ?,
                updated_at = ?
            WHERE order_id = ?
        """, (new_paid, new_balance, payment_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id))
        
        conn.commit()
    finally:
        conn.close()
    log_activity(session.get('user'), "ADD_PAYMENT", "payment", payment_id, 
                f"Payment of ₹{amount} added to order {order_id}")
    flash(f'Payment of ₹{amount} recorded successfully!', 'success')
    return redirect(f'/order_details/{order_id}')

# =====================================================
# CUSTOMER ROUTES
# =====================================================

@app.route("/customers")
@login_required
def customers():
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM customers 
            ORDER BY last_order_date DESC
        """)
        customers_list = cursor.fetchall()
    finally:
        conn.close()
    
    return render_template("customers_list.html", customers=customers_list)

@app.route("/customer_details/<customer_id>")
@login_required
def customer_details(customer_id):
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
        customer = cursor.fetchone()
        
        if not customer:
            flash('Customer not found', 'warning')
            return redirect('/customers')
        
        cursor.execute("""
            SELECT * FROM orders 
            WHERE customer_id = ? 
            ORDER BY created_at DESC
        """, (customer_id,))
        orders_list = cursor.fetchall()
        
        return render_template("customer_details.html", customer=customer, orders=orders_list)
    finally:
        conn.close()

# =====================================================
# REPORTS & ANALYTICS
# =====================================================

@app.route("/reports")
@login_required
@role_required('owner', 'manager')
def reports():
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Revenue by product type
        cursor.execute("""
            SELECT product_type, 
                   COUNT(*) as orders,
                   SUM(total_cost) as revenue
            FROM orders
            WHERE status != 'Cancelled'
            GROUP BY product_type
        """)
        revenue_by_product = cursor.fetchall()
        
        # Top customers
        cursor.execute("""
            SELECT name, mobile, total_orders, total_spent
            FROM customers
            ORDER BY total_spent DESC
            LIMIT 10
        """)
        top_customers = cursor.fetchall()
        
        # Order type distribution
        cursor.execute("""
            SELECT order_type, COUNT(*) as count
            FROM orders
            GROUP BY order_type
        """)
        order_type_dist = cursor.fetchall()
        
        return render_template("reports.html",
                             revenue_by_product=revenue_by_product,
                             top_customers=top_customers,
                             order_type_dist=order_type_dist)
    finally:
        conn.close()

# =====================================================
# ADMIN ROUTES
# =====================================================

@app.route("/admin/orders")
@login_required
@role_required('owner', 'manager')
def admin_orders():
    conn = sqlite3.connect("factory.db")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Fetch orders that need approval or have pending payments
        cursor.execute("""
            SELECT * FROM orders 
            WHERE status = 'Order placed' OR payment_status != 'Paid'
            ORDER BY created_at DESC
        """)
        orders = cursor.fetchall()
        
        return render_template("admin_orders.html", orders=orders)
    finally:
        conn.close()

@app.route("/admin/approve_order", methods=["POST"])
@login_required
@role_required('owner', 'manager')
def admin_approve_order():
    order_id = request.form["order_id"]
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT order_type FROM orders WHERE order_id = ?", (order_id,))
        order_type = cursor.fetchone()[0]
        
        workflow = WORKFLOW_STEPS.get(order_type, [])
        try:
            current_index = workflow.index('Order placed')
            new_status = workflow[current_index + 1]
        except (ValueError, IndexError):
            new_status = 'Processing'

        cursor.execute("UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?", 
                      (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id))
        conn.commit()
        
        log_activity(session.get('user'), "APPROVE", "order", order_id, f"Order approved. Status: {new_status}")
        flash(f'Order {order_id} approved. Status is now "{new_status}".', 'success')
    finally:
        conn.close()
    return redirect(url_for('admin_orders'))

@app.route("/admin/update_payment_status", methods=["POST"])
@login_required
@role_required('owner', 'manager')
def admin_update_payment_status():
    order_id = request.form["order_id"]
    new_payment_status = request.form["payment_status"]
    
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET payment_status = ?, updated_at = ? WHERE order_id = ?", 
                      (new_payment_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id))
        conn.commit()
        
        log_activity(session.get('user'), "UPDATE_PAYMENT_STATUS", "order", order_id, f"Payment status set to: {new_payment_status}")
        flash(f'Payment status for order {order_id} updated to "{new_payment_status}".', 'success')
    finally:
        conn.close()
    return redirect(url_for('admin_orders'))


# =====================================================
# API ENDPOINTS
# =====================================================

@app.route("/api/stats")
@login_required
def api_stats():
    conn = sqlite3.connect("factory.db")
    try:
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM customers")
        total_customers = cursor.fetchone()[0]
        
        cursor.execute("SELECT COALESCE(SUM(total_cost), 0) FROM orders WHERE status != 'Cancelled'")
        total_revenue = cursor.fetchone()[0]
        
        cursor.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")
        status_counts = {row[0]: row[1] for row in cursor.fetchall()}
        
        return jsonify({
            "total_orders": total_orders,
            "total_customers": total_customers,
            "total_revenue": total_revenue,
            "status_counts": status_counts
        })
    finally:
        conn.close()

@app.route("/api/products")
@login_required
def api_products():
    product_type = request.args.get('type')
    dimensions = sorted([rate[1] for rate in PRODUCT_RATES if rate[0] == product_type])
    return jsonify(dimensions)


# =====================================================
# RUN APP
# =====================================================

if __name__ == "__main__":
    # This is for development only. A production server (like Gunicorn) will be used for deployment.
    app.run(debug=True, host='0.0.0.0', port=5000)
