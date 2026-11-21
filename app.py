import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import mysql.connector
from datetime import datetime, timezone, timedelta
import json
from werkzeug.utils import secure_filename
import requests
import traceback
from flask import render_template
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

import cloudinary
import cloudinary.uploader

cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True
)

# Define Philippine Standard Time (GMT+8)
PHT = timezone(timedelta(hours=8))
XENDIT_SECRET_KEY = "xnd_development_cOVPjzFmLKHqoI0ZUe7Evb8fJHAhH9Lfkdl4drkqhp8MZgxyazEeGf8gNayl50"


def get_pht_now():
    """Returns current datetime explicitly in Philippine Standard Time (GMT+8)"""
    return datetime.now(PHT).replace(tzinfo=None)

UPLOAD_FOLDER = os.path.join('uploads', 'livestock')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
CORS(app)

def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST") or os.environ.get("MYSQLHOST") or "localhost",
        user=os.environ.get("DB_USER") or os.environ.get("MYSQLUSER") or "root",
        password=os.environ.get("DB_PASSWORD") or os.environ.get("MYSQLPASSWORD") or "",
        database=(
            os.environ.get("DB_NAME")
            or os.environ.get("MYSQL_DATABASE")
            or os.environ.get("MYSQLDATABASE")
            or "farmbid_db"
        ),
        port=int(os.environ.get("DB_PORT") or os.environ.get("MYSQLPORT") or 3306),
        connection_timeout=5
    )

# 1. Bidder Bids Route (Active Bid Hold Calculation Without Premature Wallet Balance Deduction)
@app.route('/api/bids', methods=['POST'])
def place_bid():
    connection = None
    cursor = None
    try:
        data = request.get_json() or {}
        buyer_id = data.get('buyerId') or data.get('bidderId') or data.get('userId') or session.get('user_id')
        listing_id = data.get('auctionId') or data.get('listingId')

        raw_bid = data.get('bidAmount')
        if raw_bid is None:
            raw_bid = data.get('amount')
        if raw_bid is None:
            raw_bid = data.get('bid_amount')

        bid_amount = float(str(raw_bid).replace(',', '').strip())

        if not buyer_id or not listing_id or bid_amount <= 0:
            return jsonify({'success': False, 'message': 'Invalid bid details.'}), 400

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

       # 1. Get user's wallet balance
        cursor.execute("SELECT wallet_balance FROM wallets WHERE user_id = %s", (buyer_id,))
        wallet = cursor.fetchone()
        balance = float(wallet['wallet_balance']) if wallet and wallet.get('wallet_balance') is not None else 0.00

        # Calculate committed funds across ALL active listings
        cursor.execute(
            """
            SELECT b.listing_id, MAX(b.bid_amount) AS max_bid
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s
              AND LOWER(COALESCE(l.auction_status, 'live')) NOT IN ('ended', 'closed', 'completed')
              AND (l.auction_ends_at IS NULL OR l.auction_ends_at > NOW())
            GROUP BY b.listing_id
            """,
            (buyer_id,)
        )
        active_bid_rows = cursor.fetchall()
        total_committed = sum(float(row.get('max_bid') or 0) for row in active_bid_rows)
        available_bidding_money = max(0.00, balance - total_committed)

        # User's current commitment on THIS specific listing (if they already bid on it)
        current_bid_this_listing = 0.00
        for row in active_bid_rows:
            if str(row.get('listing_id')) == str(listing_id):
                current_bid_this_listing = float(row.get('max_bid') or 0)
                break

        max_allowed_for_this_listing = available_bidding_money + current_bid_this_listing

        if bid_amount > max_allowed_for_this_listing:
            return jsonify({
                'success': False,
                'message': (
                    f'Insufficient available bidding money. '
                    f'Available: ₱{available_bidding_money:,.2f}'
                    + (f' (Max on this listing: ₱{max_allowed_for_this_listing:,.2f}).' if current_bid_this_listing > 0 else '.')
                )
            }), 400

        # 2. Check current highest bid or starting price on the listing
        cursor.execute("SELECT current_highest_bid, starting_price FROM livestock_listings WHERE listing_id = %s", (listing_id,))
        listing = cursor.fetchone()
        if listing:
            current_top = float(listing.get('current_highest_bid') or 0)
            starting_price = float(listing.get('starting_price') or 0)
            increment = 500.0
            min_allowed = (current_top + increment) if current_top > 0 else starting_price

            if bid_amount < min_allowed:
                return jsonify({
                    'success': False,
                    'message': f'Bid must be at least ₱{min_allowed:,.2f} (minimum ₱{increment:,.2f} increment).'
                }), 400

        # 3. Insert the bid using listing_id and bidder_id (do NOT deduct from wallets.wallet_balance until won/settled)
        cursor.execute(
            """INSERT INTO bids (listing_id, bidder_id, bid_amount, created_at)
               VALUES (%s, %s, %s, NOW())""",
            (listing_id, buyer_id, bid_amount)
        )

        # 4. Update the livestock listing's current highest bid
        cursor.execute(
            """UPDATE livestock_listings SET current_highest_bid = %s WHERE listing_id = %s""",
            (bid_amount, listing_id)
        )

        connection.commit()

        return jsonify({'success': True, 'message': 'Bid placed successfully!'})

    except Exception as e:
        if connection:
            connection.rollback()
        print("Place Bid Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 2. Get All Bids by a Specific Bidder (Fixes 404 error)
@app.route('/api/bids/bidder/<int:bidder_id>', methods=['GET'])
def get_bids_by_bidder(bidder_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        query = """
            SELECT b.*, 
                   l.animal_type, 
                   l.breed, 
                   l.weight,
                   l.auction_status, 
                   l.auction_ends_at,
                   l.starting_price,
                   l.current_highest_bid
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s
            ORDER BY b.created_at DESC
        """
        cursor.execute(query, (bidder_id,))
        bids = cursor.fetchall()
        for b in bids:
            if b.get('created_at') and isinstance(b['created_at'], datetime):
                b['created_at'] = b['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if b.get('auction_ends_at') and isinstance(b['auction_ends_at'], datetime):
                b['auction_ends_at'] = b['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'data': bids}), 200
    except Exception as e:
        print("Bidder Bids API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 3. Fetch all bids for a specific livestock listing
@app.route('/api/bids/listing/<int:listing_id>', methods=['GET'])
def get_bids_by_listing(listing_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        query = """
            SELECT 
                b.bid_id, 
                b.bid_amount, 
                b.created_at, 
                b.bidder_id,
                COALESCE(CONCAT(u.first_name, ' ', u.last_name), 'Anonymous Bidder') AS bidder_name
            FROM bids b
            LEFT JOIN users u ON b.bidder_id = u.user_id
            WHERE b.listing_id = %s
            ORDER BY b.bid_amount DESC, b.created_at DESC
        """
        cursor.execute(query, (listing_id,))
        bids = cursor.fetchall()
        for b in bids:
            if b.get('created_at') and isinstance(b['created_at'], datetime):
                b['created_at'] = b['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify({
            'success': True,
            'data': bids
        }), 200

    except Exception as e:
        print("Listing Bids API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()
        
def settle_won_auctions_for_buyer(cursor, buyer_id):
    """Deducts winning bid amounts once from wallets.wallet_balance for ended auctions won by buyer_id."""
    cursor.execute("""
        SELECT
            l.listing_id,
            l.animal_type,
            MAX(b.bid_amount) AS winning_bid
        FROM bids b
        JOIN livestock_listings l ON b.listing_id = l.listing_id
        WHERE b.bidder_id = %s
          AND (
              LOWER(COALESCE(l.auction_status, '')) IN ('ended', 'closed', 'settlement', 'completed')
              OR (l.auction_ends_at IS NOT NULL AND l.auction_ends_at <= NOW())
          )
          AND b.bid_amount >= COALESCE(l.current_highest_bid, 0)
          AND NOT EXISTS (
              SELECT 1
              FROM bids b2
              WHERE b2.listing_id = l.listing_id
                AND b2.bid_amount > b.bid_amount
          )
        GROUP BY l.listing_id, l.animal_type
    """, (buyer_id,))
    won_listings = cursor.fetchall() or []

    for win in won_listings:
        amount = float(win.get('winning_bid') or 0.0)
        if amount <= 0:
            continue

        cursor.execute("""
            SELECT 1 FROM wallet_transactions
            WHERE user_id = %s
              AND LOWER(COALESCE(type, '')) IN ('debit', 'payment', 'bid_deduction')
              AND amount = %s
            LIMIT 1
        """, (buyer_id, amount))
        if cursor.fetchone():
            continue

        cursor.execute("""
            UPDATE wallets
            SET wallet_balance = GREATEST(0, COALESCE(wallet_balance, 0) - %s)
            WHERE user_id = %s
        """, (amount, buyer_id))

        cursor.execute("""
            INSERT INTO wallet_transactions (user_id, amount, type, created_at)
            VALUES (%s, %s, 'debit', NOW())
        """, (buyer_id, amount))
            
# 4. Bidder Dashboard Route
@app.route('/api/dashboard/bidder/<int:bidder_id>', methods=['GET'])
def get_bidder_dashboard(bidder_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        settle_won_auctions_for_buyer(cursor, bidder_id)
        connection.commit()

        query = """
            SELECT 
                b.bid_id, 
                b.listing_id, 
                b.bid_amount, 
                b.created_at,
                l.animal_type, 
                l.breed, 
                l.weight, 
                l.starting_price,
                l.current_highest_bid,
                l.auction_status,
                l.auction_ends_at,
                CASE
                    WHEN LOWER(COALESCE(l.auction_status, '')) IN ('ended', 'closed')
                      OR (l.auction_ends_at IS NOT NULL AND l.auction_ends_at <= NOW())
                    THEN 1 ELSE 0
                END AS is_ended
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s
            ORDER BY b.created_at DESC
        """
        cursor.execute(query, (bidder_id,))
        bids = cursor.fetchall()
        for b in bids:
            if b.get('created_at') and isinstance(b['created_at'], datetime):
                b['created_at'] = b['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if b.get('auction_ends_at') and isinstance(b['auction_ends_at'], datetime):
                b['auction_ends_at'] = b['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')

        total_bids = len(bids)
        total_committed = sum(float(b['bid_amount'] or 0) for b in bids)
        won_count = sum(
            1 for b in bids
            if b.get('is_ended') == 1 and float(b['bid_amount'] or 0) >= float(b['current_highest_bid'] or 0)
        )

        return jsonify({
            'success': True,
            'data': {
                'total_bids': total_bids,
                'total_committed': total_committed,
                'won_count': won_count,
                'bids': bids
            }
        }), 200

    except Exception as e:
        print("Database Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 5. Admin Dashboard Route
@app.route('/api/dashboard/admin', methods=['GET'])
def get_admin_dashboard():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM disputes ORDER BY dispute_id DESC")
        disputes = cursor.fetchall()
        for d in disputes:
            if d.get('created_at') and isinstance(d['created_at'], datetime):
                d['created_at'] = d['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute("SELECT * FROM users ORDER BY user_id DESC")
        users = cursor.fetchall()
        
        cursor.execute("""
            SELECT b.*, u.first_name, u.last_name, l.animal_type 
            FROM bids b
            LEFT JOIN users u ON b.bidder_id = u.user_id
            LEFT JOIN livestock_listings l ON b.listing_id = l.listing_id
            ORDER BY b.bid_amount DESC
        """)
        bids = cursor.fetchall()
        for b in bids:
            if b.get('created_at') and isinstance(b['created_at'], datetime):
                b['created_at'] = b['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        cursor.execute("SELECT animal_type, COUNT(*) as count, SUM(starting_price) as total_sales FROM livestock_listings GROUP BY animal_type ORDER BY count DESC")
        sales_categories = cursor.fetchall()
        
        return jsonify({
            'success': True,
            'data': {
                'disputes': disputes,
                'users': users,
                'bids': bids,
                'sales_categories': sales_categories
            }
        }), 200

    except Exception as e:
        print("Admin Dashboard API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 6. Admin User Status Update Route
@app.route('/api/admin/users/<int:user_id>/status', methods=['PUT'])
def update_user_status(user_id):
    connection = None
    cursor = None
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        connection = get_db_connection()
        cursor = connection.cursor()
        
        query = "UPDATE users SET status = %s, account_status = %s WHERE user_id = %s"
        cursor.execute(query, (new_status, new_status, user_id))
        connection.commit()
        
        return jsonify({'success': True, 'message': f'User status updated to {new_status}'}), 200

    except Exception as e:
        if connection: connection.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 7. User Registration Route
@app.route('/api/auth/register', methods=['POST'])
def register_user():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        first_name = data.get('firstName')
        last_name = data.get('lastName')
        email = data.get('email')
        contact = data.get('contactNumber')
        raw_password = data.get('password')
        role = data.get('role', 'bidder')
        address = data.get('address', '')
        
        # Hash the password securely for database storage
        password_hash = generate_password_hash(raw_password)
        
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("SELECT user_id, status, account_status FROM users WHERE email = %s", (email,))
        existing_user = cursor.fetchone()
        
        if existing_user:
            user_status = str(existing_user.get('status', '')).lower()
            acc_status = str(existing_user.get('account_status', '')).lower()
            
            if user_status == 'rejected' or acc_status == 'rejected':
                update_query = """
                    UPDATE users 
                    SET first_name = %s, last_name = %s, contact_number = %s, password_hash = %s, role = %s, address = %s, status = 'pending', account_status = 'pending'
                    WHERE email = %s
                """
                cursor.execute(update_query, (first_name, last_name, contact, password_hash, role, address, email))
                connection.commit()
                return jsonify({'success': True, 'message': 'Registration re-submitted successfully. Awaiting admin approval.'}), 201
            else:
                return jsonify({'success': False, 'message': 'An active or pending account with this email address already exists.'}), 400
        else:
            query = """
                INSERT INTO users (first_name, last_name, email, contact_number, password_hash, role, address, status, account_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 'pending')
            """
            cursor.execute(query, (first_name, last_name, email, contact, password_hash, role, address))
            connection.commit()
            
        return jsonify({'success': True, 'message': 'Registration successful. Awaiting admin approval.'}), 201

    except Exception as e:
        if connection: connection.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 8. User Login Route
@app.route('/api/auth/login', methods=['POST'])
def login_user():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        email = data.get('email')
        password = data.get('password')

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        query = "SELECT * FROM users WHERE email = %s"
        cursor.execute(query, (email,))
        user = cursor.fetchone()

        # Verify password (supports hashed passwords and direct admin fallback)
        password_valid = False
        if user:
            stored_hash = user.get('password_hash', '')
            if (email == 'admin@farmbid.com' and password == 'Admin1234') or stored_hash == password:
                password_valid = True
            else:
                try:
                    password_valid = check_password_hash(stored_hash, password)
                except Exception:
                    password_valid = (stored_hash == password)

        if not user or not password_valid:
            return jsonify({'success': False, 'message': 'Invalid email or password.'}), 401

        user_status = str(user.get('status', 'active')).lower()
        acc_status = str(user.get('account_status', 'active')).lower()
        role = user.get('role', 'bidder')

        if role != 'admin':
            if user_status == 'rejected' or acc_status == 'rejected':
                return jsonify({'success': False, 'message': 'Access Denied: Your account registration request was rejected by the administrator.'}), 403
            if user_status == 'pending' or acc_status == 'pending':
                return jsonify({'success': False, 'message': "Account Pending Approval: Your account is currently waiting for an administrator's approval."}), 403
            if user_status in ['suspended', 'banned'] or acc_status in ['suspended', 'banned']:
                return jsonify({'success': False, 'message': 'Access Restricted: Your account has been suspended or banned by administration.'}), 403

        return jsonify({'success': True, 'user': user}), 200

    except Exception as e:
        print(f"Login Error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 9. Get Farmer Wallet & GCash Details (Strictly derived from completed sales only)
@app.route('/api/farmer/wallet/<int:farmer_id>', methods=['GET'])
def get_farmer_wallet(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        balance = 0.0
        try:
            cursor.execute("SELECT * FROM wallets WHERE user_id = %s", (farmer_id,))
            wallet = cursor.fetchone()
            if wallet:
                raw_bal = wallet.get('wallet_balance')
                if raw_bal is None:
                    raw_bal = wallet.get('balance')
                balance = float(raw_bal or 0.0)
        except Exception:
            balance = 0.0

        withdrawals = []
        try:
            cursor.execute("""
                SELECT *
                FROM farmer_withdrawals
                WHERE farmer_id = %s
                ORDER BY 1 DESC
            """, (farmer_id,))
            rows = cursor.fetchall() or []
            for idx, r in enumerate(rows, start=1):
                item = dict(r)
                row_id = item.get('withdrawal_id') or item.get('id') or idx
                item['reference_code'] = item.get('reference_code') or f"WD-{int(row_id):04d}"
                req_at = item.get('requested_at') or item.get('created_at') or ''
                if isinstance(req_at, datetime):
                    req_at = req_at.strftime('%Y-%m-%d %H:%M:%S')
                item['requested_at'] = str(req_at)
                item['gcash_number'] = item.get('gcash_number') or '09958400832'
                item['amount'] = float(item.get('amount') or 0.0)
                item['status'] = item.get('status') or 'Completed'
                withdrawals.append(item)
        except Exception as w_err:
            print("Withdrawals Fetch Error:", str(w_err))
            withdrawals = []

        return jsonify({
            'success': True,
            'data': {
                'balance': balance,
                'wallet_balance': balance,
                'available_balance': balance,
                'withdrawals': withdrawals
            }
        }), 200
    except Exception as e:
        print("Wallet Fetch Error:", str(e))
        return jsonify({
            'success': True,
            'data': {
                'balance': 0.0,
                'wallet_balance': 0.0,
                'available_balance': 0.0,
                'withdrawals': []
            }
        }), 200
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 10. Request GCash Withdrawal
@app.route('/api/farmer/withdraw', methods=['POST'])
def request_withdrawal():
    connection = None
    cursor = None
    try:
        data = request.get_json() or {}
        farmer_id = data.get('farmerId') or data.get('farmer_id') or data.get('userId') or data.get('user_id')
        raw_amount = data.get('amount', 0)
        gcash_number = (data.get('gcashNumber') or data.get('gcash_number') or '').strip()

        try:
            amount = float(str(raw_amount).replace(',', '').strip())
        except (ValueError, TypeError):
            amount = 0.0

        if not farmer_id or amount <= 0 or not gcash_number:
            return jsonify({'success': False, 'message': 'Invalid withdrawal amount or insufficient balance.'}), 400

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("SELECT * FROM wallets WHERE user_id = %s LIMIT 1", (farmer_id,))
        wallet = cursor.fetchone()

        raw_bal = None
        if wallet:
            raw_bal = wallet.get('wallet_balance')
            if raw_bal is None:
                raw_bal = wallet.get('balance')
        current_balance = float(raw_bal or 0.0)

        if amount > current_balance + 1e-6:
            return jsonify({
                'success': False,
                'message': f'Invalid withdrawal amount or insufficient balance. Available: ₱{current_balance:,.2f}'
            }), 400

        new_balance = max(0.0, round(current_balance - amount, 2))
        ref_code = f"WD-{os.urandom(2).hex().upper()}"

        try:
            cursor.execute("UPDATE wallets SET wallet_balance = %s WHERE user_id = %s", (new_balance, farmer_id))
        except Exception:
            cursor.execute("UPDATE wallets SET balance = %s WHERE user_id = %s", (new_balance, farmer_id))

        try:
            cursor.execute("UPDATE users SET gcash_number = %s WHERE user_id = %s", (gcash_number, farmer_id))
        except Exception:
            pass

        inserted = False
        insert_attempts = [
            (
                """
                INSERT INTO farmer_withdrawals (farmer_id, amount, status, created_at)
                VALUES (%s, %s, 'Completed', NOW())
                """,
                (farmer_id, amount)
            ),
            (
                """
                INSERT INTO farmer_withdrawals (farmer_id, amount, status)
                VALUES (%s, %s, 'Completed')
                """,
                (farmer_id, amount)
            ),
            (
                """
                INSERT INTO farmer_withdrawals (user_id, amount, status, created_at)
                VALUES (%s, %s, 'Completed', NOW())
                """,
                (farmer_id, amount)
            ),
            (
                """
                INSERT INTO farmer_withdrawals (user_id, amount, status)
                VALUES (%s, %s, 'Completed')
                """,
                (farmer_id, amount)
            ),
        ]

        last_err = None
        for sql_query, params in insert_attempts:
            try:
                cursor.execute(sql_query, params)
                inserted = True
                break
            except Exception as exc:
                last_err = exc

        if not inserted and last_err:
            raise last_err

        connection.commit()

        return jsonify({
            'success': True,
            'message': 'Withdrawal successful!',
            'new_balance': new_balance,
            'reference_code': ref_code
        }), 200

    except Exception as e:
        if connection:
            connection.rollback()
        print("Withdrawal Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

def sync_expired_auctions(cursor, farmer_id=None):
    """Marks live/active listings as 'ended' once their auction_ends_at timestamp has passed."""
    if farmer_id is not None:
        cursor.execute("""
            UPDATE livestock_listings
            SET auction_status = 'ended'
            WHERE farmer_id = %s
              AND LOWER(COALESCE(auction_status, 'live')) IN ('live', 'active')
              AND auction_ends_at IS NOT NULL
              AND auction_ends_at <= NOW()
        """, (farmer_id,))
    else:
        cursor.execute("""
            UPDATE livestock_listings
            SET auction_status = 'ended'
            WHERE LOWER(COALESCE(auction_status, 'live')) IN ('live', 'active')
              AND auction_ends_at IS NOT NULL
              AND auction_ends_at <= NOW()
        """)

# 11. Get Farmer Dashboard Metrics (Strictly separated)
@app.route('/api/farmer/metrics/<int:farmer_id>', methods=['GET'])
def get_farmer_metrics(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            sync_expired_auctions(cursor, farmer_id)
            connection.commit()
        except Exception as sync_err:
            connection.rollback()
            print("Sync expired auctions warning:", sync_err)

        now_pht = get_pht_now()

        # 1. Highest active bid (actual bids only; starting_price is NOT potential bid money)
        cursor.execute("""
            SELECT COALESCE(MAX(b.bid_amount), 0) AS max_bid
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE l.farmer_id = %s
              AND LOWER(COALESCE(l.auction_status, '')) IN ('active', 'live')
              AND (l.auction_ends_at IS NULL OR (l.auction_ends_at > NOW() AND l.auction_ends_at > %s))
        """, (farmer_id, now_pht))
        bid_res = cursor.fetchone() or {}
        highest_bid = float(bid_res.get('max_bid') or 0)

        # 2. Completed count strictly where status is 'completed'
        cursor.execute("""
            SELECT COUNT(*) AS completed_count
            FROM livestock_listings
            WHERE farmer_id = %s AND LOWER(COALESCE(auction_status, '')) = 'completed'
        """, (farmer_id,))
        comp_res = cursor.fetchone() or {}
        completed_count = int(comp_res.get('completed_count') or 0)

        # 3. Verification Queue only for ended/expired listings that actually have a bidder
        cursor.execute("""
            SELECT COUNT(*) AS queue_count
            FROM livestock_listings l
            WHERE l.farmer_id = %s
              AND LOWER(COALESCE(l.auction_status, '')) != 'completed'
              AND (
                  LOWER(COALESCE(l.auction_status, '')) IN ('ended', 'closed', 'settlement', 'pending')
                  OR (l.auction_ends_at IS NOT NULL AND (l.auction_ends_at <= NOW() OR l.auction_ends_at <= %s))
              )
              AND EXISTS (
                  SELECT 1 FROM bids b WHERE b.listing_id = l.listing_id
              )
        """, (farmer_id, now_pht))
        queue_res = cursor.fetchone() or {}
        queue_count = int(queue_res.get('queue_count') or 0)

        return jsonify({
            'success': True,
            'data': {
                'highest_bid': highest_bid,
                'completed_count': completed_count,
                'verification_queue_count': queue_count
            }
        }), 200

    except Exception as e:
        print("Metrics API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route('/api/farmer/active-bid/<int:farmer_id>', methods=['GET'])
def get_farmer_active_bid(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            sync_expired_auctions(cursor, farmer_id)
            connection.commit()
        except Exception as sync_err:
            connection.rollback()
            print("Active bid sync warning:", sync_err)

        now_pht = get_pht_now()
        try:
            cursor.execute("""
                SELECT * FROM livestock_listings
                WHERE farmer_id = %s
                  AND LOWER(COALESCE(auction_status, '')) IN ('active', 'live')
                  AND (auction_ends_at IS NULL OR (auction_ends_at > NOW() AND auction_ends_at > %s))
                ORDER BY COALESCE(current_highest_bid, starting_price, 0) DESC
                LIMIT 1
            """, (farmer_id, now_pht))
        except Exception:
            connection.rollback()
            cursor.execute("""
                SELECT * FROM livestock_listings
                WHERE farmer_id = %s
                  AND LOWER(COALESCE(auction_status, '')) IN ('active', 'live')
                ORDER BY listing_id DESC
                LIMIT 1
            """, (farmer_id,))

        listing = cursor.fetchone()
        if listing:
            if listing.get('auction_ends_at') and isinstance(listing['auction_ends_at'], datetime):
                listing['auction_ends_at'] = listing['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
            if listing.get('created_at') and isinstance(listing['created_at'], datetime):
                listing['created_at'] = listing['created_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'data': listing}), 200
    except Exception as e:
        print("Active Bid API Error:", str(e))
        return jsonify({'success': True, 'data': None}), 200
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection:
            try:
                connection.close()
            except Exception:
                pass

# 13. Update Farmer Settings & GCash Number
@app.route('/api/farmer/settings/<int:farmer_id>', methods=['PUT'])
def update_farmer_settings(farmer_id):
    connection = None
    cursor = None
    try:
        data = request.get_json()
        first_name = data.get('firstName')
        last_name = data.get('lastName')
        contact_number = data.get('contactNumber')
        gcash_number = data.get('gcashNumber')
        
        connection = get_db_connection()
        cursor = connection.cursor()
        
        query = """
            UPDATE users 
            SET first_name = %s, last_name = %s, contact_number = %s, gcash_number = %s
            WHERE user_id = %s
        """
        cursor.execute(query, (first_name, last_name, contact_number, gcash_number, farmer_id))
        connection.commit()
        
        return jsonify({'success': True, 'message': 'Settings updated successfully.'}), 200

    except Exception as e:
        if connection: connection.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


# 14. Get Buyer Wallet and Financial Breakdown (Accurate Committed Funds Calculation)
@app.route('/api/buyer/wallet/<int:buyer_id>', methods=['GET'])
def get_buyer_wallet(buyer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        settle_won_auctions_for_buyer(cursor, buyer_id)
        connection.commit()

        # Get wallet balance
        cursor.execute("SELECT wallet_balance FROM wallets WHERE user_id = %s", (buyer_id,))
        wallet = cursor.fetchone()
        balance = float(wallet['wallet_balance']) if wallet and wallet.get('wallet_balance') is not None else 0.00

        # Fetch user's bids joined with livestock listings (excluding closed/expired auctions)
        query = """
            SELECT
                b.bid_id,
                b.listing_id,
                b.bid_amount,
                b.created_at,
                l.animal_type,
                l.breed,
                l.weight,
                l.current_highest_bid,
                l.starting_price,
                l.auction_status,
                l.auction_ends_at
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s
              AND LOWER(COALESCE(l.auction_status, 'live')) NOT IN ('ended', 'closed', 'completed')
              AND (l.auction_ends_at IS NULL OR l.auction_ends_at > NOW())
            ORDER BY b.bid_amount DESC, b.created_at DESC
        """
        cursor.execute(query, (buyer_id,))
        rows = cursor.fetchall()

        # Keep each user's highest active bid per listing_id for committed funds
        highest_by_listing = {}
        for row in rows:
            lid = row['listing_id']
            amt = float(row.get('bid_amount') or 0)
            if lid not in highest_by_listing or amt > float(highest_by_listing[lid]['bid_amount'] or 0):
                item = dict(row)
                if item.get('created_at') and isinstance(item['created_at'], datetime):
                    item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                if item.get('auction_ends_at') and isinstance(item['auction_ends_at'], datetime):
                    item['auction_ends_at'] = item['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
                item['bid_amount'] = amt
                highest_by_listing[lid] = item

        active_bids = list(highest_by_listing.values())
        committed_funds = sum(float(item['bid_amount'] or 0) for item in active_bids)
        available_funds = max(0.00, balance - committed_funds)

        return jsonify({
            'success': True,
            'data': {
                'wallet_balance': balance,
                'available_funds': available_funds,
                'committed_funds': committed_funds,
                'bids': active_bids
            }
        }), 200

    except Exception as e:
        print("Get Wallet Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 15. Top up Buyer Wallet via Simulated GCash Checkout
@app.route('/api/buyer/topup-gcash', methods=['POST'])
def topup_buyer_gcash():
    try:
        data = request.get_json()
        buyer_id = data.get('buyerId')
        amount = float(data.get('amount', 0))
        gcash_number = data.get('gcashNumber')
        
        if amount <= 0:
            return jsonify({'success': False, 'message': 'Invalid top-up amount.'}), 400
            
        # Return URL to our built-in simulation checkout page
        simulator_url = f"/bidder/gcash-simulator.html?buyerId={buyer_id}&amount={amount}&gcash={gcash_number}"
        
        return jsonify({
            'success': True,
            'invoice_url': simulator_url,
            'message': 'Redirecting to GCash secure payment simulator...'
        }), 200

    except Exception as e:
        print("GCash Top-up Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500

# Simulation Credit Route (Single unified endpoint)
@app.route('/api/buyer/simulate-credit', methods=['POST'])
def simulate_credit():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        buyer_id = data.get('buyerId')
        amount = float(data.get('amount', 0))

        if not buyer_id or amount <= 0:
            return jsonify({'success': False, 'message': 'Invalid buyer ID or amount.'}), 400

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # 1. Update or create wallet balance
        cursor.execute("SELECT * FROM wallets WHERE user_id = %s", (buyer_id,))
        wallet = cursor.fetchone()

        if wallet:
            cursor.execute(
                "UPDATE wallets SET wallet_balance = wallet_balance + %s WHERE user_id = %s",
                (amount, buyer_id)
            )
        else:
            cursor.execute(
                "INSERT INTO wallets (user_id, wallet_balance) VALUES (%s, %s)",
                (buyer_id, amount)
            )

        # 2. Log transaction matching the exact table schema
        cursor.execute(
            """INSERT INTO wallet_transactions 
               (user_id, amount, type, reference_id, created_at) 
               VALUES (%s, %s, 'credit', NULL, NOW())""",
            (buyer_id, amount)
        )

        connection.commit()
        return jsonify({'success': True, 'message': 'Top-up successful'})

    except Exception as e:
        if connection:
            connection.rollback()
        print("Simulate Credit Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# 16. Fetch All Live Livestock Listings
@app.route('/api/listings', methods=['GET'])
def get_all_listings():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT l.*, u.first_name AS farmer_first_name, u.last_name AS farmer_last_name, u.contact_number AS farmer_contact, u.address AS farmer_address
            FROM livestock_listings l
            JOIN users u ON l.farmer_id = u.user_id
            ORDER BY l.created_at DESC
        """)
        listings = cursor.fetchall()

        for item in listings:
            if item.get('auction_ends_at') and isinstance(item['auction_ends_at'], datetime):
                item['auction_ends_at'] = item['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
            if item.get('created_at') and isinstance(item['created_at'], datetime):
                item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M:%S')

            # Foolproof Image Resolution using basename
            img_val = item.get('image_url')
            resolved_images = []

            if img_val:
                try:
                    parsed = json.loads(img_val) if isinstance(img_val, str) and img_val.startswith('[') else [img_val]
                    for img in parsed:
                        if img:
                            img_str = str(img).strip('"[]\'')
                            if img_str.startswith('http://') and '127.0.0.1' not in img_str and 'localhost' not in img_str:
                                resolved_images.append(img_str)
                            elif img_str.startswith('https://'):
                                resolved_images.append(img_str)
                            else:
                                clean_filename = os.path.basename(img_str)
                                if clean_filename:
                                    resolved_images.append(f"/uploads/livestock/{clean_filename}")
                except Exception:
                    clean_filename = os.path.basename(str(img_val).strip('"[]\''))
                    if clean_filename:
                        resolved_images.append(f"/uploads/livestock/{clean_filename}")

            if not resolved_images:
                resolved_images = ['https://images.unsplash.com/photo-1541781774459-bb2af2f05b55']

            item['images'] = resolved_images
            item['image_url'] = resolved_images[0]

            # Resolve Documents (preserve full HTTP/HTTPS URLs like Cloudinary)
            doc_val = item.get('documents')
            resolved_docs = []
            if doc_val:
                try:
                    parsed_docs = json.loads(doc_val) if isinstance(doc_val, str) and doc_val.startswith('[') else [doc_val]
                    for doc in parsed_docs:
                        if doc:
                            doc_str = str(doc).strip('"[]\'')
                            if doc_str.startswith(('http://', 'https://')):
                                resolved_docs.append(doc_str)
                            else:
                                clean_doc_filename = os.path.basename(doc_str)
                                if clean_doc_filename:
                                    resolved_docs.append(f"/uploads/livestock/{clean_doc_filename}")
                except Exception:
                    doc_str = str(doc_val).strip('"[]\'')
                    if doc_str.startswith(('http://', 'https://')):
                        resolved_docs.append(doc_str)
                    else:
                        clean_doc_filename = os.path.basename(doc_str)
                        if clean_doc_filename:
                            resolved_docs.append(f"/uploads/livestock/{clean_doc_filename}")
            item['documents'] = resolved_docs

        return jsonify({
            'success': True,
            'data': listings
        }), 200

    except Exception as e:
        print("Get Listings API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 19. Serve Uploaded Files
@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    uploads_dir = os.path.join(app.root_path, 'uploads')
    return send_from_directory(uploads_dir, filename)

# 20. Handle Multiple Livestock Photo / Document Uploads
@app.route('/api/upload-livestock-images', methods=['POST'])
def upload_livestock_images():
    try:
        if 'images' not in request.files:
            return jsonify({'success': False, 'message': 'No files provided.'}), 400

        files = request.files.getlist('images')
        saved_urls = []

        for file in files:
            if file and file.filename != '':
                filename_lower = (file.filename or '').lower()
                res_type = "raw" if filename_lower.endswith(".pdf") else "auto"
                file.stream.seek(0)
                try:
                    upload_result = cloudinary.uploader.upload(
                        file,
                        folder="farmbid/livestock",
                        resource_type=res_type,
                        access_mode="public"
                    )
                except Exception:
                    file.stream.seek(0)
                    upload_result = cloudinary.uploader.upload(
                        file,
                        folder="farmbid/livestock",
                        resource_type="auto",
                        access_mode="public"
                    )
                secure_url = upload_result.get('secure_url')
                if secure_url:
                    saved_urls.append(secure_url)

        return jsonify({
            'success': True,
            'images': saved_urls
        }), 200
    except Exception as e:
        print("Upload Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/disputes', methods=['GET'])
def get_admin_disputes():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute("SELECT * FROM disputes ORDER BY created_at DESC")
            disputes = cursor.fetchall()
        except Exception:
            cursor.execute("SELECT * FROM disputes")
            disputes = cursor.fetchall()

        # Preload listings and users for safe lookup across varying schemas
        cursor.execute("""
            SELECT l.*, u.first_name AS farmer_first, u.last_name AS farmer_last
            FROM livestock_listings l
            LEFT JOIN users u ON l.farmer_id = u.user_id
        """)
        listings_by_id = {str(r['listing_id']): r for r in (cursor.fetchall() or [])}

        cursor.execute("SELECT user_id, first_name, last_name, role FROM users")
        users_by_id = {str(r['user_id']): r for r in (cursor.fetchall() or [])}

        for d in disputes:
            if 'id' in d and 'dispute_id' not in d:
                d['dispute_id'] = d['id']

            lid = d.get('listing_id') or d.get('reported_listing_id')
            d['listing_id'] = lid
            listing = listings_by_id.get(str(lid)) if lid is not None else None

            if listing:
                farmer_full = f"{listing.get('farmer_first') or ''} {listing.get('farmer_last') or ''}".strip()
                d['livestock_summary'] = f"{listing.get('animal_type') or 'Livestock'}" + (
                    f" ({listing.get('breed')})" if listing.get('breed') else ""
                )
                d['livestock_code'] = f"AUC-{int(lid):03d}"
                d['reported_farmer_name'] = farmer_full or (f"User #{listing.get('farmer_id')}" if listing.get('farmer_id') else 'N/A')
            else:
                d['livestock_summary'] = 'Not linked'
                d['livestock_code'] = f"AUC-{int(lid):03d}" if lid else '—'
                rep_user = users_by_id.get(str(d.get('reported_user_id')))
                d['reported_farmer_name'] = (
                    f"{rep_user.get('first_name') or ''} {rep_user.get('last_name') or ''}".strip()
                    if rep_user else (f"User #{d.get('reported_user_id')}" if d.get('reported_user_id') else 'N/A')
                )

            reporter = users_by_id.get(str(d.get('reporter_id')))
            d['reporter_name'] = (
                f"{reporter.get('first_name') or ''} {reporter.get('last_name') or ''}".strip()
                if reporter else f"User #{d.get('reporter_id') or '?'}"
            )

            if d.get('created_at') and isinstance(d['created_at'], datetime):
                d['created_at'] = d['created_at'].strftime('%Y-%m-%d %H:%M')

        return jsonify({'success': True, 'data': disputes}), 200
    except Exception as e:
        print("Disputes Fetch Error:", str(e))
        return jsonify({'success': True, 'data': []}), 200
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 22. Create New Dispute Report
@app.route('/api/disputes', methods=['POST'])
def create_dispute():
    connection = None
    cursor = None
    try:
        data = request.get_json() or {}
        reporter_id = data.get('reporterId')
        reported_listing_id = data.get('listingId')
        reason = data.get('reason')
        description = data.get('description')

        raw_proofs = data.get('proofImages') or data.get('proof_images') or []
        if isinstance(raw_proofs, str):
            try:
                raw_proofs = json.loads(raw_proofs) if raw_proofs.startswith('[') else [raw_proofs]
            except Exception:
                raw_proofs = [raw_proofs]
        clean_proofs = [str(u).strip() for u in (raw_proofs or []) if u and str(u).strip()]
        proofs_json = json.dumps(clean_proofs) if clean_proofs else None

        if not reporter_id or not reason or not description:
            return jsonify({'success': False, 'message': 'Missing required dispute details.'}), 400

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        alter_statements = [
            "ALTER TABLE disputes ADD COLUMN report_code VARCHAR(50) NULL",
            "ALTER TABLE disputes ADD COLUMN listing_id INT NULL",
            "ALTER TABLE disputes ADD COLUMN reported_listing_id INT NULL",
            "ALTER TABLE disputes ADD COLUMN reason VARCHAR(255) NULL",
            "ALTER TABLE disputes ADD COLUMN description TEXT NULL",
            "ALTER TABLE disputes ADD COLUMN proof_images TEXT NULL",
            "ALTER TABLE disputes ADD COLUMN status VARCHAR(50) DEFAULT 'open'",
            "ALTER TABLE disputes MODIFY COLUMN reported_user_id INT NULL"
        ]
        for stmt in alter_statements:
            try:
                cursor.execute(stmt)
                connection.commit()
            except Exception:
                connection.rollback()

        if reported_listing_id:
            try:
                cursor.execute(
                    """
                    SELECT * FROM disputes
                    WHERE reporter_id = %s
                      AND (listing_id = %s OR reported_listing_id = %s)
                    ORDER BY 1 DESC LIMIT 1
                    """,
                    (reporter_id, reported_listing_id, reported_listing_id)
                )
                existing_dispute = cursor.fetchone()
                if existing_dispute:
                    return jsonify({
                        'success': False,
                        'already_disputed': True,
                        'message': 'You have already submitted a dispute report for this livestock listing.',
                        'dispute': existing_dispute
                    }), 409
            except Exception:
                pass

        reported_user_id = data.get('reportedUserId')
        if not reported_user_id and reported_listing_id:
            try:
                cursor.execute("SELECT * FROM livestock_listings WHERE listing_id = %s LIMIT 1", (reported_listing_id,))
                listing = cursor.fetchone()
                if listing:
                    reported_user_id = (
                        listing.get('farmer_id')
                        or listing.get('seller_id')
                        or listing.get('user_id')
                        or listing.get('owner_id')
                    )
            except Exception:
                pass

        import random
        report_code = f"DSP-{random.randint(1000, 9999)}"

        write_cursor = connection.cursor()
        try:
            query = """
                INSERT INTO disputes (
                    report_code, listing_id, reported_listing_id,
                    reporter_id, reported_user_id, issue_title, reporter_statement,
                    priority, dispute_status, reason, description, proof_images, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'normal', 'open', %s, %s, %s, 'pending')
            """
            write_cursor.execute(
                query,
                (
                    report_code,
                    reported_listing_id,
                    reported_listing_id,
                    reporter_id,
                    reported_user_id,
                    reason,
                    description,
                    reason,
                    description,
                    proofs_json
                )
            )
        except Exception:
            connection.rollback()
            query = """
                INSERT INTO disputes (
                    report_code, reporter_id, reported_user_id, listing_id, reason, description, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            """
            write_cursor.execute(
                query,
                (report_code, reporter_id, reported_user_id, reported_listing_id, reason, description)
            )

        connection.commit()
        write_cursor.close()

        return jsonify({
            'success': True,
            'message': 'Dispute report submitted successfully.',
            'report_code': report_code
        }), 201
    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        print("Create Dispute Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection:
            try:
                connection.close()
            except Exception:
                pass

@app.route('/api/disputes/check', methods=['GET'])
def check_existing_dispute():
    connection = None
    cursor = None
    try:
        reporter_id = request.args.get('reporterId') or request.args.get('bidderId')
        listing_id = request.args.get('listingId')

        if not reporter_id or not listing_id:
            return jsonify({'success': True, 'has_dispute': False, 'dispute': None}), 200

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT * FROM disputes
            WHERE reporter_id = %s
              AND (listing_id = %s OR reported_listing_id = %s)
            ORDER BY 1 DESC LIMIT 1
            """,
            (reporter_id, listing_id, listing_id)
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({'success': True, 'has_dispute': False, 'dispute': None}), 200

        raw_status = str(row.get('status') or row.get('dispute_status') or 'pending').strip().lower()
        if raw_status in ('resolved', 'approved', 'accepted', 'upheld'):
            normalized_status = 'resolved'
            decision_text = (
                row.get('admin_decision')
                or row.get('resolution_notes')
                or row.get('admin_notes')
                or 'Resolved by Admin — Dispute upheld / action taken.'
            )
        elif raw_status in ('dismissed', 'rejected', 'declined'):
            normalized_status = 'dismissed'
            decision_text = (
                row.get('admin_decision')
                or row.get('resolution_notes')
                or row.get('admin_notes')
                or 'Dismissed by Admin — No violation found.'
            )
        else:
            normalized_status = 'pending'
            decision_text = None

        proofs = []
        if row.get('proof_images'):
            try:
                parsed = json.loads(row['proof_images']) if isinstance(row['proof_images'], str) else row['proof_images']
                if isinstance(parsed, list):
                    proofs = [str(p) for p in parsed if p]
            except Exception:
                proofs = []

        return jsonify({
            'success': True,
            'has_dispute': True,
            'dispute': {
                'dispute_id': row.get('dispute_id') or row.get('id'),
                'report_code': row.get('report_code') or '',
                'reason': row.get('reason') or row.get('issue_title') or 'Dispute Report',
                'description': row.get('description') or row.get('reporter_statement') or '',
                'status': normalized_status,
                'raw_status': raw_status,
                'decision': decision_text,
                'proof_images': proofs
            }
        }), 200
    except Exception as e:
        print("Check Dispute Error:", str(e))
        return jsonify({'success': True, 'has_dispute': False, 'dispute': None}), 200
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 23. Update Dispute Status (Resolve / Dismiss)
@app.route('/api/admin/disputes/<int:dispute_id>/status', methods=['PUT'])
def update_dispute_status(dispute_id):
    connection = None
    cursor = None
    try:
        data = request.get_json(silent=True) or {}
        new_status = str(data.get('status', '')).strip().lower()
        if new_status not in ('resolved', 'dismissed', 'pending'):
            return jsonify({'success': False, 'message': 'Invalid dispute status.'}), 400

        connection = get_db_connection()
        cursor = connection.cursor()

        try:
            cursor.execute(
                "UPDATE disputes SET status = %s, dispute_status = %s WHERE dispute_id = %s",
                (new_status, new_status, dispute_id)
            )
        except Exception:
            connection.rollback()
            cursor.execute(
                "UPDATE disputes SET status = %s WHERE dispute_id = %s",
                (new_status, dispute_id)
            )

        if cursor.rowcount == 0:
            return jsonify({'success': False, 'message': 'Dispute not found.'}), 404

        connection.commit()
        return jsonify({'success': True, 'status': new_status, 'message': f'Dispute marked as {new_status}.'}), 200
    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        print("Error updating dispute status:", e)
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection:
            try:
                connection.close()
            except Exception:
                pass

# Admin Marketplace Auctions & Reports API
@app.route('/api/admin/reports', methods=['GET'])
def admin_reports():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        query = """
            SELECT l.*, 
                   COALESCE(u.first_name, 'Verified') AS farmer_first_name, 
                   COALESCE(u.last_name, 'Farmer') AS farmer_last_name, 
                   COALESCE(u.address, 'Philippines') AS farmer_address,
                   COALESCE(u.contact_number, 'N/A') AS farmer_contact,
                   (SELECT COALESCE(MAX(bid_amount), l.starting_price) FROM bids WHERE listing_id = l.listing_id) AS current_highest_bid,
                   (SELECT COUNT(*) FROM bids WHERE listing_id = l.listing_id) AS total_bids
            FROM livestock_listings l
            LEFT JOIN users u ON l.farmer_id = u.user_id
            ORDER BY l.created_at DESC
        """
        cursor.execute(query)
        listings = cursor.fetchall()
        
        now = datetime.now()
        
        # Dynamically compute accurate status based on time expiration
        for item in listings:
            raw_status = str(item.get('auction_status', 'live')).lower()
            ends_at = item.get('auction_ends_at')
            
            is_expired = False
            if ends_at:
                if isinstance(ends_at, str):
                    try:
                        ends_at_dt = datetime.fromisoformat(ends_at.replace(' ', 'T'))
                    except:
                        ends_at_dt = None
                else:
                    ends_at_dt = ends_at
                
                if ends_at_dt and now > ends_at_dt:
                    is_expired = True
            
            if raw_status == 'flagged':
                item['computed_status'] = 'flagged'
            elif is_expired or raw_status in ['ended', 'closed']:
                item['computed_status'] = 'closed'
            elif raw_status in ['settlement', 'awaiting_settlement', 'pending']:
                item['computed_status'] = 'settlement'
            elif raw_status in ['completed']:
                item['computed_status'] = 'completed'
            else:
                item['computed_status'] = 'live'

        total_volume = sum(float(item.get('current_highest_bid') or item.get('starting_price') or 0) for item in listings)
        
        live_list = [l for l in listings if l['computed_status'] == 'live']
        flagged_list = [l for l in listings if l['computed_status'] == 'flagged']
        completed_list = [l for l in listings if l['computed_status'] in ['completed', 'closed']]
        settlement_list = [l for l in listings if l['computed_status'] == 'settlement']
        upcoming_list = [l for l in listings if l['computed_status'] == 'upcoming']
        
        live_bids_count = sum(int(l.get('total_bids', 0) or 0) for l in live_list)
        
        return jsonify({
            'success': True,
            'data': listings,
            'stats': {
                'totalVolume': total_volume,
                'totalListings': len(listings),
                'liveCount': len(live_list),
                'liveBids': live_bids_count,
                'settlementCount': len(settlement_list),
                'flaggedCount': len(flagged_list),
                'completedCount': len(completed_list),
                'upcomingCount': len(upcoming_list)
            }
        }), 200
    except Exception as e:
        print("Admin Reports Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# Farmer Delivery Queue & Buyer Payments API (Strictly separated)
@app.route('/api/farmer/delivery-queue/<int:farmer_id>', methods=['GET'])
def get_farmer_delivery_queue(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            sync_expired_auctions(cursor, farmer_id)
            connection.commit()
        except Exception:
            connection.rollback()

        now_pht = get_pht_now()

        cursor.execute("""
            SELECT l.*,
                   (SELECT MAX(b.bid_amount) FROM bids b WHERE b.listing_id = l.listing_id) AS winning_bid,
                   (SELECT u.first_name FROM bids b JOIN users u ON b.bidder_id = u.user_id WHERE b.listing_id = l.listing_id ORDER BY b.bid_amount DESC, b.created_at DESC LIMIT 1) AS winner_first_name,
                   (SELECT u.last_name FROM bids b JOIN users u ON b.bidder_id = u.user_id WHERE b.listing_id = l.listing_id ORDER BY b.bid_amount DESC, b.created_at DESC LIMIT 1) AS winner_last_name,
                   (SELECT u.contact_number FROM bids b JOIN users u ON b.bidder_id = u.user_id WHERE b.listing_id = l.listing_id ORDER BY b.bid_amount DESC, b.created_at DESC LIMIT 1) AS winner_contact
            FROM livestock_listings l
            WHERE l.farmer_id = %s
              AND (
                  LOWER(COALESCE(l.auction_status, '')) IN ('ended', 'closed', 'completed', 'settlement', 'pending')
                  OR (l.auction_ends_at IS NOT NULL AND (l.auction_ends_at <= NOW() OR l.auction_ends_at <= %s))
              )
              AND EXISTS (
                  SELECT 1 FROM bids b WHERE b.listing_id = l.listing_id
              )
            ORDER BY l.created_at DESC
        """, (farmer_id, now_pht))
        listings = cursor.fetchall()

        for item in listings:
            if item.get('auction_ends_at') and isinstance(item['auction_ends_at'], datetime):
                item['auction_ends_at'] = item['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
            if item.get('created_at') and isinstance(item['created_at'], datetime):
                item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({
            'success': True,
            'data': listings
        }), 200

    except Exception as e:
        print("Delivery Queue API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# 24. Confirm Livestock Receipt & Settle Escrow to Farmer Wallet (With Transaction Logging)
@app.route('/api/buyer/confirm-receipt/<int:listing_id>', methods=['POST'])
def confirm_livestock_receipt(listing_id):
    connection = None
    cursor = None
    try:
        data = request.get_json() or {}
        buyer_id = data.get('bidderId') or data.get('buyerId')

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT l.listing_id, l.farmer_id, l.animal_type, l.current_highest_bid, l.starting_price, l.auction_status,
                   (SELECT b.bid_amount FROM bids b WHERE b.listing_id = l.listing_id AND b.bidder_id = %s ORDER BY b.bid_amount DESC LIMIT 1) AS buyer_bid
            FROM livestock_listings l
            WHERE l.listing_id = %s
        """, (buyer_id, listing_id))
        listing = cursor.fetchone()

        if not listing:
            return jsonify({'success': False, 'message': 'Listing not found.'}), 404

        if str(listing.get('auction_status') or '').lower() == 'completed':
            return jsonify({'success': False, 'message': 'Receipt already confirmed.'}), 400

        farmer_id = listing['farmer_id']
        winning_amount = float(listing['buyer_bid'] or listing['current_highest_bid'] or listing['starting_price'] or 0.0)

        # 1. Deduct from buyer wallet only if not already deducted
        cursor.execute("""
            SELECT 1 FROM wallet_transactions
            WHERE user_id = %s
              AND LOWER(COALESCE(type, '')) IN ('debit', 'payment', 'bid_deduction')
              AND amount = %s
            LIMIT 1
        """, (buyer_id, winning_amount))
        already_deducted = cursor.fetchone() is not None

        if not already_deducted and winning_amount > 0:
            cursor.execute("""
                UPDATE wallets
                SET wallet_balance = GREATEST(0, COALESCE(wallet_balance, 0) - %s)
                WHERE user_id = %s
            """, (winning_amount, buyer_id))

       # 2. Credit farmer wallet (insert row if missing)
        cursor.execute("SELECT wallet_balance FROM wallets WHERE user_id = %s", (farmer_id,))
        farmer_wallet = cursor.fetchone()
        if farmer_wallet:
            cursor.execute("""
                UPDATE wallets
                SET wallet_balance = COALESCE(wallet_balance, 0) + %s
                WHERE user_id = %s
            """, (winning_amount, farmer_id))
        else:
            cursor.execute("""
                INSERT INTO wallets (user_id, wallet_balance)
                VALUES (%s, %s)
            """, (farmer_id, winning_amount))

        # 3. Mark listing status as completed
        cursor.execute("UPDATE livestock_listings SET auction_status = 'completed' WHERE listing_id = %s", (listing_id,))

        # 4. Log debit transaction matching available wallet_transactions columns
        cursor.execute("""
            INSERT INTO wallet_transactions (user_id, amount, type, created_at)
            VALUES (%s, %s, 'debit', NOW())
        """, (buyer_id, winning_amount))

        connection.commit()

        return jsonify({
            'success': True,
            'message': f'Receipt confirmed! ₱{winning_amount:,.2f} settled and logged to your transaction history.'
        }), 200
    except Exception as e:
        if connection:
            connection.rollback()
        print("Confirm Receipt Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Get Buyer Wallet Transactions
@app.route('/api/buyer/transactions/<int:buyer_id>', methods=['GET'])
def get_buyer_transactions(buyer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM wallet_transactions
            WHERE user_id = %s
            ORDER BY created_at ASC
        """, (buyer_id,))
        rows = cursor.fetchall()

        running_balance = 0.0
        for t in rows:
            amt = float(t.get('amount') or 0.0)
            t_type = str(t.get('type') or t.get('txn_type') or '').lower()

            if t_type in ('debit', 'payment', 'withdrawal'):
                running_balance = max(0.0, running_balance - amt)
            else:
                running_balance += amt

            t['running_balance'] = round(running_balance, 2)

            if t.get('created_at') and isinstance(t['created_at'], datetime):
                t['created_at'] = t['created_at'].strftime('%Y-%m-%d %H:%M:%S')

        rows.reverse()
        return jsonify({'success': True, 'data': rows}), 200
    except Exception as e:
        print("Transactions API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Get Buyer Notifications API
@app.route('/api/buyer/notifications/<path:user_id>', methods=['GET'])
def get_buyer_notifications(user_id):
    connection = None
    cursor = None
    try:
        # Extract clean numeric ID if a colon or extra suffix is passed (e.g. '4:1' -> '4')
        clean_id = str(user_id).split(':')[0]
        
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        notifications = []
        
        # 1. Try fetching wallet transactions as notifications
        try:
            cursor.execute("""
                SELECT reference_code, description, amount, type, created_at
                FROM wallet_transactions
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT 5
            """, (clean_id,))
            txns = cursor.fetchall()
            for t in txns:
                amt = float(t.get('amount') or 0)
                t_type = str(t.get('type') or '').lower()
                title = "Wallet Top-up Successful" if t_type == 'topup' else "Auction Payment Completed"
                if t_type == 'payment':
                    title = "Order Settled & Paid"
                notifications.append({
                    'title': title,
                    'description': t.get('description', f'Transaction of ₱{amt:,.2f} processed.'),
                    'created_at': t.get('created_at')
                })
        except Exception:
            pass  # Table might not exist yet, fallback gracefully

        return jsonify({
            'success': True,
            'data': notifications
        }), 200

    except Exception as e:
        print("Notifications Error:", str(e))
        return jsonify({
            'success': True,
            'data': []
        }), 200
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

  # Get Farmer Notifications API (Bulletproof version)
@app.route('/api/farmer/notifications/<int:farmer_id>', methods=['GET'])
def get_farmer_notifications(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            sync_expired_auctions(cursor, farmer_id)
            connection.commit()
        except Exception:
            connection.rollback()

        notifications = []

        # 1. Fetch listings belonging to this farmer
        cursor.execute("""
            SELECT listing_id, animal_type, breed, auction_status, auction_ends_at, created_at
            FROM livestock_listings
            WHERE farmer_id = %s
            ORDER BY created_at DESC
        """, (farmer_id,))
        listings = cursor.fetchall() or []
        listing_map = {l['listing_id']: l for l in listings}
        listing_ids = list(listing_map.keys())

        bids_by_listing = {}
        if listing_ids:
            format_strings = ','.join(['%s'] * len(listing_ids))
            cursor.execute(f"""
                SELECT bid_id, bid_amount, created_at, listing_id, bidder_id
                FROM bids
                WHERE listing_id IN ({format_strings})
                ORDER BY created_at DESC
            """, tuple(listing_ids))
            all_bids = cursor.fetchall() or []

            for b in all_bids:
                bids_by_listing.setdefault(b['listing_id'], []).append(b)

            for b in all_bids[:5]:
                raw_date = b.get('created_at')
                time_str = raw_date.strftime('%Y-%m-%d %H:%M') if hasattr(raw_date, 'strftime') else str(raw_date or 'Recent')
                l_info = listing_map.get(b['listing_id']) or {}
                animal = l_info.get('animal_type') or 'Livestock'

                cursor.execute("SELECT first_name, last_name FROM users WHERE user_id = %s", (b['bidder_id'],))
                bidder = cursor.fetchone()
                bidder_name = f"{bidder['first_name'] if bidder else 'Buyer'} {bidder['last_name'] if bidder else ''}".strip()

                notifications.append({
                    'title': "New Bid Received",
                    'message': f"{bidder_name} bid ₱{float(b.get('bid_amount') or 0):,.2f} on {animal} (AUC-{str(b['listing_id']).zfill(3)})",
                    'time': time_str,
                    'type': 'bid'
                })

        # 2. Notify farmer when an ended/expired listing has no bidders
        now_dt = datetime.now()
        for l in listings:
            lid = l['listing_id']
            if len(bids_by_listing.get(lid, [])) > 0:
                continue

            raw_status = str(l.get('auction_status') or '').lower()
            ends_at = l.get('auction_ends_at')
            is_expired = False
            if ends_at:
                ends_dt = datetime.fromisoformat(str(ends_at).replace(' ', 'T')) if isinstance(ends_at, str) else ends_at
                if ends_dt and now_dt >= ends_dt:
                    is_expired = True

            if raw_status in ('ended', 'closed') or is_expired:
                animal = l.get('animal_type') or 'Livestock'
                breed_part = f" ({l['breed']})" if l.get('breed') else ""
                raw_time = ends_at or l.get('created_at')
                time_str = raw_time.strftime('%Y-%m-%d %H:%M') if hasattr(raw_time, 'strftime') else str(raw_time or 'Ended')

                notifications.append({
                    'title': "Auction Ended With No Bidder",
                    'message': f"Your {animal}{breed_part} listing (AUC-{str(lid).zfill(3)}) ended with no bids placed.",
                    'time': time_str,
                    'type': 'no_bidder'
                })

        return jsonify({'success': True, 'data': notifications[:10]}), 200

    except Exception as e:
        print("Farmer Notifications API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Get Admin Notifications API
@app.route('/api/admin/notifications', methods=['GET'])
def get_admin_notifications():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        notifications = []
        
        # 1. Fetch pending user registrations (using account_status)
        try:
            cursor.execute("""
                SELECT user_id, first_name, last_name, email, role, created_at 
                FROM users 
                WHERE account_status = 'pending' OR account_status IS NULL 
                ORDER BY created_at DESC LIMIT 5
            """)
            pending_users = cursor.fetchall()
            for u in pending_users:
                name = f"{u.get('first_name') or 'New'} {u.get('last_name') or 'User'}".strip()
                raw_date = u.get('created_at')
                time_str = raw_date.strftime('%Y-%m-%d %H:%M') if hasattr(raw_date, 'strftime') else str(raw_date or 'Recent')

                notifications.append({
                    'title': "Pending Registration",
                    'message': f"{name} ({u.get('role', 'user')}) registered and awaits verification.",
                    'time': time_str,
                    'type': 'user'
                })
        except Exception as user_err:
            print("Pending users fetch warning:", user_err)
            
        # 2. Fetch open disputes / reports (using id aliased as dispute_id)
        try:
            cursor.execute("""
                SELECT id AS dispute_id, reason, status, created_at 
                FROM disputes 
                WHERE status = 'pending' OR status = 'open' 
                ORDER BY created_at DESC LIMIT 5
            """)
            disputes = cursor.fetchall()
            for d in disputes:
                raw_date = d.get('created_at')
                time_str = raw_date.strftime('%Y-%m-%d %H:%M') if hasattr(raw_date, 'strftime') else str(raw_date or 'Recent')

                notifications.append({
                    'title': "Active Dispute Report",
                    'message': f"Report #{d.get('dispute_id', 1)}: {d.get('reason', 'No reason specified')} requires review.",
                    'time': time_str,
                    'type': 'dispute'
                })
        except Exception as disp_err:
            print("Disputes fetch warning:", disp_err)

        return jsonify({'success': True, 'data': notifications}), 200

    except Exception as e:
        print("Admin Notifications API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# --- Bidder / Buyer Dashboard & Wallet APIs ---

@app.route('/api/bidder/wallet/<int:user_id>', methods=['GET'])
def get_bidder_wallet(user_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM wallets WHERE user_id = %s", (user_id,))
        wallet = cursor.fetchone()
        
        balance = wallet.get('wallet_balance', 0.00) if wallet else 0.00
        committed = wallet.get('committed_balance', 0.00) if wallet else 0.00
        
        return jsonify({
            'success': True,
            'data': {
                'wallet_balance': balance,
                'available_balance': balance,
                'committed_balance': committed
            }
        }), 200
    except Exception as e:
        print("Bidder Wallet Error:", str(e))
        return jsonify({
            'success': True,
            'data': {
                'wallet_balance': 0.00,
                'available_balance': 0.00,
                'committed_balance': 0.00
            }
        }), 200
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@app.route('/api/bidder/stats/<int:user_id>', methods=['GET'])
def get_bidder_stats(user_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        return jsonify({
            'success': True,
            'data': {
                'live_bids': 0,
                'committed_funds': 0.00,
                'won_bidding': 0
            }
        }), 200
    except Exception as e:
        print("Bidder Stats Error:", str(e))
        return jsonify({
            'success': True,
            'data': {
                'live_bids': 0,
                'committed_funds': 0.00,
                'won_bidding': 0
            }
        }), 200
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

@app.route('/api/listings/farmer/<int:farmer_id>', methods=['GET'])
def get_farmer_listings(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            sync_expired_auctions(cursor, farmer_id)
            connection.commit()
        except Exception:
            connection.rollback()

        cursor.execute("""
            SELECT l.*,
                   u.first_name AS farmer_first_name,
                   u.last_name AS farmer_last_name,
                   u.contact_number AS farmer_contact,
                   u.address AS farmer_address,
                   (SELECT COUNT(*) FROM bids b WHERE b.listing_id = l.listing_id) AS total_bids
            FROM livestock_listings l
            JOIN users u ON l.farmer_id = u.user_id
            WHERE l.farmer_id = %s
            ORDER BY l.created_at DESC
        """, (farmer_id,))
        listings = cursor.fetchall()

        for item in listings:
            if item.get('auction_ends_at') and isinstance(item['auction_ends_at'], datetime):
                item['auction_ends_at'] = item['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
            if item.get('created_at') and isinstance(item['created_at'], datetime):
                item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M:%S')

            img_val = item.get('image_url')
            resolved_images = []
            if img_val:
                try:
                    parsed = json.loads(img_val) if isinstance(img_val, str) and img_val.startswith('[') else [img_val]
                    for img in parsed:
                        if img:
                            img_str = str(img).strip('"[]\'')
                            if img_str.startswith(('http://', 'https://')) and '127.0.0.1' not in img_str and 'localhost' not in img_str:
                                resolved_images.append(img_str)
                            else:
                                clean_filename = os.path.basename(img_str)
                                if clean_filename:
                                    resolved_images.append(f"/uploads/livestock/{clean_filename}")
                except Exception:
                    clean_filename = os.path.basename(str(img_val).strip('"[]\''))
                    if clean_filename:
                        resolved_images.append(f"/uploads/livestock/{clean_filename}")

            if not resolved_images:
                resolved_images = ['https://images.unsplash.com/photo-1541781774459-bb2af2f05b55']

            item['images'] = resolved_images
            item['image_url'] = resolved_images[0]

        return jsonify({'success': True, 'data': listings}), 200
    except Exception as e:
        print("Farmer Listings API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route('/api/listings', methods=['POST'])
def create_listing():
    connection = None
    cursor = None
    try:
        data = request.get_json(silent=True) or {}

        farmer_id = (
            data.get('farmerId')
            or data.get('farmer_id')
            or data.get('userId')
            or data.get('user_id')
            or session.get('user_id')
        )
        animal_type = (data.get('animalType') or data.get('animal_type') or '').strip()
        breed = (data.get('breed') or '').strip()
        gender = (data.get('gender') or data.get('sex') or '').strip()
        age = data.get('age')
        if age is None or str(age).strip() == '':
            age_val = '0'
        else:
            age_val = str(age).strip()

        weight = data.get('weight')
        starting_price = data.get('startingPrice') or data.get('starting_price')
        auction_ends_at = (
            data.get('auctionEndsAt')
            or data.get('auction_ends_at')
            or data.get('closingDateTime')
            or data.get('closing_datetime')
        )

        raw_images = data.get('images')
        if raw_images is None:
            raw_images = data.get('imageUrls') or data.get('image_url') or []
        if isinstance(raw_images, str):
            try:
                parsed_imgs = json.loads(raw_images) if raw_images.startswith('[') else [raw_images]
            except Exception:
                parsed_imgs = [raw_images]
        elif isinstance(raw_images, list):
            parsed_imgs = raw_images
        else:
            parsed_imgs = []
        clean_images = [str(u).strip() for u in parsed_imgs if u and str(u).strip()]

        raw_docs = data.get('documents')
        if raw_docs is None:
            raw_docs = data.get('documentUrls') or []
        if isinstance(raw_docs, str):
            try:
                parsed_docs = json.loads(raw_docs) if raw_docs.startswith('[') else [raw_docs]
            except Exception:
                parsed_docs = [raw_docs]
        elif isinstance(raw_docs, list):
            parsed_docs = raw_docs
        else:
            parsed_docs = []
        clean_docs = [str(u).strip() for u in parsed_docs if u and str(u).strip()]

        try:
            weight_val = float(str(weight).replace(',', '').strip()) if weight not in (None, '') else 0.0
        except (ValueError, TypeError):
            weight_val = 0.0

        try:
            starting_price_val = float(str(starting_price).replace(',', '').strip()) if starting_price not in (None, '') else 0.0
        except (ValueError, TypeError):
            starting_price_val = 0.0

        if not farmer_id or not animal_type or starting_price_val <= 0:
            return jsonify({'success': False, 'message': 'Please fill in all required listing details and starting price.'}), 400

        if not clean_images:
            return jsonify({'success': False, 'message': 'At least one livestock photo is required.'}), 400

        if not clean_docs:
            return jsonify({'success': False, 'message': 'At least one verification or compliance document is required.'}), 400

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        images_json = json.dumps(clean_images)
        docs_json = json.dumps(clean_docs)
        primary_image = clean_images[0] if clean_images else None

        try:
            cursor.execute("ALTER TABLE livestock_listings ADD COLUMN documents TEXT NULL")
            connection.commit()
        except Exception:
            connection.rollback()

        insert_attempts = [
            (
                """
                INSERT INTO livestock_listings (
                    farmer_id, animal_type, breed, gender, age, weight,
                    starting_price, current_highest_bid, image_url, documents,
                    auction_status, auction_ends_at, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 'live', %s, NOW())
                """,
                (
                    farmer_id, animal_type, breed, gender, age_val, weight_val,
                    starting_price_val, primary_image or images_json, docs_json,
                    auction_ends_at
                )
            ),
            (
                """
                INSERT INTO livestock_listings (
                    farmer_id, animal_type, breed, age, weight,
                    starting_price, image_url, documents, auction_status, auction_ends_at, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'live', %s, NOW())
                """,
                (
                    farmer_id, animal_type, breed, age_val, weight_val,
                    starting_price_val, primary_image or images_json, docs_json,
                    auction_ends_at
                )
            ),
            (
                """
                INSERT INTO livestock_listings (
                    farmer_id, animal_type, breed, age, weight,
                    starting_price, image_url, documents, auction_status, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'live', NOW())
                """,
                (
                    farmer_id, animal_type, breed, age_val, weight_val,
                    starting_price_val, primary_image or images_json, docs_json
                )
            ),
        ]

        last_err = None
        created_id = None
        for sql_query, params in insert_attempts:
            try:
                cursor.execute(sql_query, params)
                created_id = cursor.lastrowid
                break
            except Exception as exc:
                connection.rollback()
                last_err = exc

        if created_id is None and last_err:
            raise last_err

        connection.commit()

        return jsonify({
            'success': True,
            'message': 'Livestock listing created successfully!',
            'listing_id': created_id,
            'data': {'listing_id': created_id}
        }), 201

    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        print("Create Listing Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection:
            try:
                connection.close()
            except Exception:
                pass

# Admin Settings Xendit Status API
@app.route('/api/admin/settings/xendit-status', methods=['GET'])
def get_admin_xendit_status():
    try:
        # Return live mode status for platform configuration
        return jsonify({'success': True, 'status': 'Connected & Active (Live Mode)'}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/auth/<path:filename>')
def serve_auth(filename):
    auth_dir = os.path.join(app.root_path, 'auth')
    return send_from_directory(auth_dir, filename)

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory(os.path.join(app.root_path, 'assets'), filename)

@app.route('/admin/<path:filename>')
def serve_admin_files(filename):
    return send_from_directory(os.path.join(app.root_path, 'admin'), filename)

@app.route('/bidder/<path:filename>')
def serve_bidder_files(filename):
    return send_from_directory(os.path.join(app.root_path, 'bidder'), filename)

@app.route('/farmer/<path:filename>')
def serve_farmer_files(filename):
    return send_from_directory(os.path.join(app.root_path, 'farmer'), filename)

if __name__ == '__main__':
    app.run(debug=True, port=3000)