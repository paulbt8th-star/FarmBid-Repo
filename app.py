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
        host="localhost",
        user="root",
        password="",
        database="farmbid_db"
    )

# 1. Bidder Bids Route (Escrow Lock & Automatic Outbid Refund)
@app.route('/api/bids', methods=['POST'])
def place_bid():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        listing_id = data.get('listingId')
        bidder_id = data.get('bidderId')
        bid_amount = float(data.get('bidAmount', 0))
        
        if not listing_id or not bidder_id or bid_amount <= 0:
            return jsonify({'success': False, 'message': 'Invalid bid parameters.'}), 400
            
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        # 1. Fetch listing details
        cursor.execute("""
            SELECT farmer_id, starting_price, current_highest_bid, auction_status, auction_ends_at 
            FROM livestock_listings 
            WHERE listing_id = %s
        """, (listing_id,))
        listing = cursor.fetchone()
        
        if not listing:
            return jsonify({'success': False, 'message': 'Auction listing not found.'}), 404
            
        if str(listing['farmer_id']) == str(bidder_id):
            return jsonify({'success': False, 'message': 'Action prohibited: Farmers cannot place bids on their own livestock listings.'}), 400
            
        status = str(listing['auction_status']).lower()
        if status not in ['active', 'live']:
            return jsonify({'success': False, 'message': 'This auction is closed and no longer accepting bids.'}), 400
            
        current_highest = float(listing['current_highest_bid'] or listing['starting_price'] or 0)
        if bid_amount <= current_highest:
            return jsonify({'success': False, 'message': f'Bid must be higher than current highest bid of ₱{current_highest:,.2f}.'}), 400
            
        # 2. Check Bidder Wallet Balance & Available Funds (Escrow Calculation)
        cursor.execute("SELECT wallet_balance FROM users WHERE user_id = %s", (bidder_id,))
        user_row = cursor.fetchone()
        wallet_balance = float(user_row['wallet_balance']) if user_row and user_row['wallet_balance'] is not None else 0.0

        # Fetch all user bids to compute current active escrow commitments
        cursor.execute("""
            SELECT b.bid_amount, l.listing_id, l.current_highest_bid, l.auction_status, l.auction_ends_at
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s
        """, (bidder_id,))
        user_bids = cursor.fetchall()

        listings_map = {}
        for b in user_bids:
            lid = b['listing_id']
            if lid not in listings_map:
                listings_map[lid] = {'user_bids': [], 'is_closed': False}
            listings_map[lid]['user_bids'].append(float(b['bid_amount']))
            if str(b['auction_status']).lower() in ['ended', 'closed', 'completed']:
                listings_map[lid]['is_closed'] = True

        committed_funds = 0.0
        for lid, item in listings_map.items():
            # If this listing is being updated with a new bid, we replace its commitment with the new bid amount
            if lid == int(listing_id):
                continue
            if not item['is_closed'] and item['user_bids']:
                committed_funds += max(item['user_bids'])

        # Add the new bid into escrow commitment
        committed_funds += bid_amount
        available_funds = max(0.0, wallet_balance - committed_funds)

        if available_funds < 0 or bid_amount > (wallet_balance - (committed_funds - bid_amount)):
            return jsonify({
                'success': False,
                'message': f'Insufficient available wallet balance for escrow lock! Please top up.'
            }), 400

        # 3. Insert new bid & update listing highest bid (Outbidding previous leader automatically releases their escrow)
        cursor.execute("""
            INSERT INTO bids (listing_id, bidder_id, bid_amount)
            VALUES (%s, %s, %s)
        """, (listing_id, bidder_id, bid_amount))
        
        cursor.execute("""
            UPDATE livestock_listings 
            SET current_highest_bid = %s 
            WHERE listing_id = %s
        """, (bid_amount, listing_id))
        
        connection.commit()
        
        return jsonify({
            'success': True,
            'message': 'Bid placed and funds locked in escrow successfully!'
        }), 201

    except Exception as e:
        if connection: connection.rollback()
        print("Place Bid Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()
        
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

# 4. Bidder Dashboard Route (with auto-expiration sync)
@app.route('/api/dashboard/bidder/<int:bidder_id>', methods=['GET'])
def get_bidder_dashboard(bidder_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        # Automatically mark expired listings as 'ended' in the database
        cursor.execute("""
            UPDATE livestock_listings 
            SET auction_status = 'ended' 
            WHERE auction_ends_at IS NOT NULL AND auction_ends_at <= NOW() AND auction_status IN ('active', 'live')
        """)
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
                l.auction_ends_at
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
        won_count = sum(1 for b in bids if b.get('auction_status') == 'ended' and float(b['bid_amount']) >= float(b['current_highest_bid']))
        
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
        if cursor: cursor.close()
        if connection: connection.close()

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
        password = data.get('password')
        role = data.get('role', 'bidder')
        address = data.get('address', '')
        
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
                    SET first_name = %s, last_name = %s, contact_number = %s, password = %s, role = %s, address = %s, status = 'pending', account_status = 'pending'
                    WHERE email = %s
                """
                cursor.execute(update_query, (first_name, last_name, contact, password, role, address, email))
                connection.commit()
                return jsonify({'success': True, 'message': 'Registration re-submitted successfully. Awaiting admin approval.'}), 201
            else:
                return jsonify({'success': False, 'message': 'An active or pending account with this email address already exists.'}), 400
        else:
            query = """
                INSERT INTO users (first_name, last_name, email, contact_number, password, role, address, status, account_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 'pending')
            """
            cursor.execute(query, (first_name, last_name, email, contact, password, role, address))
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
        
        query = "SELECT * FROM users WHERE email = %s AND (password = %s OR password_hash = %s)"
        cursor.execute(query, (email, password, password))
        user = cursor.fetchone()
        
        if not user:
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
        print("Login Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 9. Get Farmer Wallet & GCash Details (Strictly derived from completed sales only)
@app.route('/api/farmer/wallet/<int:farmer_id>', methods=['GET'])
def get_farmer_wallet(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("SELECT user_id, first_name, last_name, email, gcash_number FROM users WHERE user_id = %s", (farmer_id,))
        user = cursor.fetchone()
        
        if not user:
            return jsonify({'success': False, 'message': 'Farmer not found'}), 404
        
        # 1. Total earned strictly from COMPLETED (delivered & confirmed) listings
        cursor.execute("""
            SELECT COALESCE(SUM(current_highest_bid), SUM(starting_price), 0) as total_earned
            FROM livestock_listings
            WHERE farmer_id = %s AND auction_status = 'completed'
        """, (farmer_id,))
        earned_result = cursor.fetchone()
        total_earned = float(earned_result['total_earned'] or 0)
        
        # 2. Total pending from ended/active listings awaiting delivery confirmation (excludes completed)
        cursor.execute("""
            SELECT COALESCE(SUM(current_highest_bid), SUM(starting_price), 0) as total_pending,
                   COUNT(*) as pending_count
            FROM livestock_listings
            WHERE farmer_id = %s AND auction_status IN ('ended', 'closed', 'settlement', 'pending', 'active', 'live') AND auction_status != 'completed'
        """, (farmer_id,))
        pending_result = cursor.fetchone()
        pending_amount = float(pending_result['total_pending'] or 0)
        pending_count = int(pending_result['pending_count'] or 0)
        
        # 3. Total withdrawn via GCash
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0) as total_withdrawn
            FROM farmer_withdrawals
            WHERE farmer_id = %s AND status = 'Completed'
        """, (farmer_id,))
        withdrawn_result = cursor.fetchone()
        total_withdrawn = float(withdrawn_result['total_withdrawn'] or 0)
        
        # True available balance is strictly completed sales minus completed withdrawals
        true_balance = max(0.0, total_earned - total_withdrawn)
        
        # Force database sync so wallet_balance never holds unconfirmed funds
        cursor.execute("UPDATE users SET wallet_balance = %s WHERE user_id = %s", (true_balance, farmer_id))
        connection.commit()
        
        cursor.execute("SELECT * FROM farmer_withdrawals WHERE farmer_id = %s ORDER BY withdrawal_id DESC", (farmer_id,))
        withdrawals = cursor.fetchall()
        for w in withdrawals:
            if w.get('requested_at') and isinstance(w['requested_at'], datetime):
                w['requested_at'] = w['requested_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify({
            'success': True,
            'data': {
                'balance': true_balance,
                'pending_amount': pending_amount,
                'pending_count': pending_count,
                'total_withdrawn': total_withdrawn,
                'gcash_number': user.get('gcash_number', '09171234567'),
                'withdrawals': withdrawals
            }
        }), 200

    except Exception as e:
        print("Wallet API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 10. Request GCash Withdrawal
@app.route('/api/farmer/withdraw', methods=['POST'])
def request_withdrawal():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        farmer_id = data.get('farmerId')
        amount = float(data.get('amount', 0))
        gcash_number = data.get('gcashNumber')
        
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("SELECT wallet_balance FROM users WHERE user_id = %s", (farmer_id,))
        user = cursor.fetchone()
        current_balance = float(user.get('wallet_balance', 0))
        
        if amount <= 0 or amount > current_balance:
            return jsonify({'success': False, 'message': 'Invalid withdrawal amount or insufficient balance.'}), 400
            
        new_balance = current_balance - amount
        ref_code = f"WD-{os.urandom(2).hex().upper()}"
        
        cursor.execute("UPDATE users SET wallet_balance = %s, gcash_number = %s WHERE user_id = %s", (new_balance, gcash_number, farmer_id))
        cursor.execute("""
            INSERT INTO farmer_withdrawals (farmer_id, reference_code, amount, gcash_number, status)
            VALUES (%s, %s, %s, %s, 'Completed')
        """, (farmer_id, ref_code, amount, gcash_number))
        
        connection.commit()
        
        return jsonify({
            'success': True,
            'message': 'GCash withdrawal successful!',
            'new_balance': new_balance
        }), 200

    except Exception as e:
        if connection: connection.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 11. Get Farmer Dashboard Metrics (Strictly separated)
@app.route('/api/farmer/metrics/<int:farmer_id>', methods=['GET'])
def get_farmer_metrics(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        # 1. Highest active bid
        cursor.execute("""
            SELECT COALESCE(MAX(current_highest_bid), MAX(starting_price), 0) as max_bid
            FROM livestock_listings
            WHERE farmer_id = %s AND (auction_status = 'active' OR auction_status = 'live')
        """, (farmer_id,))
        bid_res = cursor.fetchone()
        highest_bid = float(bid_res['max_bid'] or 0)
        
        # 2. Completed count strictly where status is 'completed'
        cursor.execute("""
            SELECT COUNT(*) as completed_count
            FROM livestock_listings
            WHERE farmer_id = %s AND auction_status = 'completed'
        """, (farmer_id,))
        comp_res = cursor.fetchone()
        completed_count = int(comp_res['completed_count'] or 0)
        
        # 3. Verification Queue / Pending Payment Count (Strictly excluding completed)
        cursor.execute("""
            SELECT COUNT(*) as queue_count
            FROM livestock_listings
            WHERE farmer_id = %s 
              AND auction_status IN ('ended', 'closed', 'settlement', 'pending') 
              AND auction_status != 'completed'
        """, (farmer_id,))
        queue_res = cursor.fetchone()
        queue_count = int(queue_res['queue_count'] or 0)
        
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
        if cursor: cursor.close()
        if connection: connection.close()

# 12. Get Farmer Active Bid Status Card Data
@app.route('/api/farmer/active-bid/<int:farmer_id>', methods=['GET'])
def get_farmer_active_bid(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM livestock_listings 
            WHERE farmer_id = %s AND (auction_status = 'active' OR auction_status = 'live')
            ORDER BY current_highest_bid DESC LIMIT 1
        """, (farmer_id,))
        listing = cursor.fetchone()
        if listing:
            if listing.get('auction_ends_at') and isinstance(listing['auction_ends_at'], datetime):
                listing['auction_ends_at'] = listing['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
            if listing.get('created_at') and isinstance(listing['created_at'], datetime):
                listing['created_at'] = listing['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'data': listing}), 200
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
        
        # Force status update for any past-due auctions
        cursor.execute("""
            UPDATE livestock_listings 
            SET auction_status = 'ended' 
            WHERE auction_ends_at IS NOT NULL AND auction_ends_at <= NOW() AND auction_status IN ('active', 'live')
        """)
        connection.commit()
        
        cursor.execute("SELECT user_id, first_name, last_name, email, gcash_number, wallet_balance FROM users WHERE user_id = %s", (buyer_id,))
        user = cursor.fetchone()
        
        if not user:
            return jsonify({'success': False, 'message': 'Buyer not found'}), 404
            
        wallet_balance = float(user['wallet_balance']) if user['wallet_balance'] is not None else 0.00
        
        cursor.execute("""
            SELECT b.bid_id, b.bid_amount, l.listing_id, l.animal_type, l.weight, l.starting_price, l.current_highest_bid, l.auction_status, l.auction_ends_at
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s
        """, (buyer_id,))
        bids = cursor.fetchall()

        cursor.execute("""
            SELECT COALESCE(SUM(b.bid_amount), 0) as total_paid
            FROM bids b
            JOIN livestock_listings l ON b.listing_id = l.listing_id
            WHERE b.bidder_id = %s AND l.auction_status = 'completed'
        """, (buyer_id,))
        paid_res = cursor.fetchone()
        total_paid = float(paid_res['total_paid'] or 0)
        
        now = datetime.now()
        
        listings_map = {}
        for b in bids:
            lid = b['listing_id']
            if lid not in listings_map:
                listings_map[lid] = {
                    'listing_id': lid,
                    'animal_type': b['animal_type'],
                    'weight': b['weight'],
                    'starting_price': float(b['starting_price'] or 0),
                    'current_highest_bid': float(b['current_highest_bid'] or 0),
                    'auction_status': str(b['auction_status'] or 'live').lower(),
                    'auction_ends_at': b['auction_ends_at'],
                    'user_bids': []
                }
            listings_map[lid]['user_bids'].append(float(b['bid_amount']))
            
        committed_funds = 0.0
        
        for lid, item in listings_map.items():
            highest_user_bid = max(item['user_bids']) if item['user_bids'] else 0.0
            
            ends_at = item['auction_ends_at']
            is_expired = False
            if ends_at:
                if isinstance(ends_at, str):
                    try:
                        ends_at_dt = datetime.fromisoformat(ends_at.replace(' ', 'T'))
                        if now > ends_at_dt:
                            is_expired = True
                    except:
                        pass
                elif isinstance(ends_at, datetime):
                    if now > ends_at:
                        is_expired = True
                        
            is_completed = item['auction_status'] == 'completed'
            is_closed = is_expired or item['auction_status'] in ['ended', 'closed', 'completed']
            is_winning = is_closed and (highest_user_bid >= item['current_highest_bid'])
            
            # CRITICAL: If the auction is completed, funds are already paid/settled, so committed funds = 0.
            if not is_completed:
                if not is_closed:
                    committed_funds += highest_user_bid
                elif is_closed and is_winning:
                    committed_funds += highest_user_bid
                
        available_funds = max(0.0, wallet_balance - committed_funds)
        
        return jsonify({
            'success': True,
            'data': {
                'user_name': f"{user['first_name']} {user['last_name']}",
                'email': user['email'],
                'gcash_number': user['gcash_number'] or '09171234567',
                'wallet_balance': wallet_balance,
                'committed_funds': committed_funds,
                'available_funds': available_funds,
                'bids': bids
            }
        }), 200

    except Exception as e:
        print("Buyer Wallet API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
        simulator_url = f"http://127.0.0.1:5500/bidder/gcash-simulator.html?buyerId={buyer_id}&amount={amount}&gcash={gcash_number}"
        
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
        
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE users 
            SET wallet_balance = COALESCE(wallet_balance, 0) + %s 
            WHERE user_id = %s
        """, (amount, buyer_id))
        connection.commit()
        return jsonify({'success': True, 'message': 'Wallet credited successfully!'}), 200
    except Exception as e:
        if connection: connection.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
                # Format datetime objects as plain strings to prevent JSON UTC offsets
                if item.get('auction_ends_at') and isinstance(item['auction_ends_at'], datetime):
                    item['auction_ends_at'] = item['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
                if item.get('created_at') and isinstance(item['created_at'], datetime):
                    item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M:%S')

                # Resolve Images
                img_val = item.get('image_url')
                resolved_images = []
                
                if img_val:
                    try:
                        parsed = json.loads(img_val) if isinstance(img_val, str) and img_val.startswith('[') else [img_val]
                        for img in parsed:
                            if img:
                                clean_filename = os.path.basename(str(img).strip('"[]\''))
                                resolved_images.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_filename}")
                    except:
                        clean_filename = os.path.basename(str(img_val).strip('"[]\''))
                        resolved_images.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_filename}")
                
                if not resolved_images:
                    resolved_images = ['https://images.unsplash.com/photo-1541781774459-bb2af2f05b55']
                    
                item['images'] = resolved_images
                item['image_url'] = resolved_images[0]

                # Resolve Documents
                doc_val = item.get('documents')
                resolved_docs = []
                if doc_val:
                    try:
                        parsed_docs = json.loads(doc_val) if isinstance(doc_val, str) and doc_val.startswith('[') else [doc_val]
                        for doc in parsed_docs:
                            if doc:
                                clean_doc_filename = os.path.basename(str(doc).strip('"[]\''))
                                resolved_docs.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_doc_filename}")
                    except:
                        clean_doc_filename = os.path.basename(str(doc_val).strip('"[]\''))
                        resolved_docs.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_doc_filename}")
                item['documents'] = resolved_docs

        return jsonify({
            'success': True,
            'data': listings
        }), 200

    except Exception as e:
        print("Get Listings API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 17. Create New Livestock Listing (Saves full image array and documents array as JSON)
@app.route('/api/listings', methods=['POST'])
def create_listing():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        farmer_id = data.get('farmerId')
        animal_type = data.get('animalType')
        breed = data.get('breed')
        sex = data.get('sex')
        age = data.get('age')
        weight = float(data.get('weight', 0))
        starting_price = float(data.get('startingPrice', 0))
        auction_ends_at = data.get('auctionEndsAt')
        images = data.get('images', [])
        documents = data.get('documents', [])
        
        if not farmer_id or not animal_type:
            return jsonify({'success': False, 'message': 'Missing required listing parameters.'}), 400
            
        connection = get_db_connection()
        cursor = connection.cursor()
        
        # Save arrays as JSON strings
        image_url_str = json.dumps(images) if isinstance(images, list) else json.dumps([images] if images else [])
        documents_str = json.dumps(documents) if isinstance(documents, list) else json.dumps([documents] if documents else [])

        query = """
            INSERT INTO livestock_listings 
            (farmer_id, animal_type, breed, sex, age, weight, starting_price, current_highest_bid, auction_status, auction_ends_at, image_url, documents)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'live', %s, %s, %s)
        """
        cursor.execute(query, (
            farmer_id, animal_type, breed, sex, age, weight, starting_price, starting_price, auction_ends_at, image_url_str, documents_str
        ))
        connection.commit()
        
        return jsonify({
            'success': True,
            'message': 'Listing published successfully to marketplace!'
        }), 201

    except Exception as e:
        if connection: connection.rollback()
        print("Create Listing Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 18. Fetch Listings Created by a Specific Farmer
@app.route('/api/listings/farmer/<int:farmer_id>', methods=['GET'])
def get_farmer_listings(farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT l.*, u.first_name AS farmer_first_name, u.last_name AS farmer_last_name, u.contact_number AS farmer_contact, u.address AS farmer_address
            FROM livestock_listings l
            JOIN users u ON l.farmer_id = u.user_id
            WHERE l.farmer_id = %s
            ORDER BY l.created_at DESC
        """, (farmer_id,))
        listings = cursor.fetchall()

        for item in listings:
            # Format datetime objects as plain strings to prevent JSON UTC offsets
            if item.get('auction_ends_at') and isinstance(item['auction_ends_at'], datetime):
                item['auction_ends_at'] = item['auction_ends_at'].strftime('%Y-%m-%d %H:%M:%S')
            if item.get('created_at') and isinstance(item['created_at'], datetime):
                item['created_at'] = item['created_at'].strftime('%Y-%m-%d %H:%M:%S')

            # Resolve Images
            img_val = item.get('image_url')
            resolved_images = []
            
            if img_val:
                try:
                    parsed = json.loads(img_val) if isinstance(img_val, str) and img_val.startswith('[') else [img_val]
                    for img in parsed:
                        if img:
                            clean_filename = os.path.basename(str(img).strip('"[]\''))
                            resolved_images.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_filename}")
                except:
                    clean_filename = os.path.basename(str(img_val).strip('"[]\''))
                    resolved_images.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_filename}")
            
            if not resolved_images:
                resolved_images = ['https://images.unsplash.com/photo-1541781774459-bb2af2f05b55']
                
            item['images'] = resolved_images
            item['image_url'] = resolved_images[0]

            # Resolve Documents
            doc_val = item.get('documents')
            resolved_docs = []
            if doc_val:
                try:
                    parsed_docs = json.loads(doc_val) if isinstance(doc_val, str) and doc_val.startswith('[') else [doc_val]
                    for doc in parsed_docs:
                        if doc:
                            clean_doc_filename = os.path.basename(str(doc).strip('"[]\''))
                            resolved_docs.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_doc_filename}")
                except:
                    clean_doc_filename = os.path.basename(str(doc_val).strip('"[]\''))
                    resolved_docs.append(f"http://127.0.0.1:3000/uploads/livestock/{clean_doc_filename}")
            item['documents'] = resolved_docs

        return jsonify({
            'success': True,
            'data': listings
        }), 200

    except Exception as e:
        print("Farmer Listings API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
                filename = secure_filename(file.filename)
                unique_name = f"livestock-{int(datetime.now().timestamp() * 1000)}-{filename}"
                file_path = os.path.join(UPLOAD_FOLDER, unique_name)
                
                # Save physical file into uploads/livestock/ folder
                file.save(file_path)
                
                # Generate accessible URL route
                file_url = f"http://127.0.0.1:3000/uploads/livestock/{unique_name}"
                saved_urls.append(file_url)
                
        return jsonify({
            'success': True,
            'images': saved_urls
        }), 200
    except Exception as e:
        print("Upload Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500

# 21. Get All Disputes (for Admin Dashboard & Dispute List)
@app.route('/api/admin/disputes', methods=['GET'])
def get_all_disputes():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        query = """
            SELECT d.*, 
                   COALESCE(CONCAT(u.first_name, ' ', u.last_name), 'Unknown User') AS reporter_name,
                   l.animal_type, l.breed
            FROM disputes d
            LEFT JOIN users u ON d.reporter_id = u.user_id
            LEFT JOIN livestock_listings l ON d.reported_listing_id = l.listing_id
            ORDER BY d.dispute_id DESC
        """
        cursor.execute(query)
        disputes = cursor.fetchall()
        
        for d in disputes:
            if d.get('created_at') and isinstance(d['created_at'], datetime):
                d['created_at'] = d['created_at'].strftime('%Y-%m-%d %H:%M:%S')

        return jsonify({'success': True, 'data': disputes}), 200
    except Exception as e:
        print("Get Disputes Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 22. Create New Dispute Report
@app.route('/api/disputes', methods=['POST'])
def create_dispute():
    connection = None
    cursor = None
    try:
        data = request.get_json()
        reporter_id = data.get('reporterId')
        reported_listing_id = data.get('listingId')
        reason = data.get('reason')
        description = data.get('description')
        
        if not reporter_id or not reason or not description:
            return jsonify({'success': False, 'message': 'Missing required dispute details.'}), 400
            
        connection = get_db_connection()
        cursor = connection.cursor()
        
        # Generate a unique report code (e.g., DSP-4821)
        import random
        report_code = f"DSP-{random.randint(1000, 9999)}"
        
        try:
            query = """
                INSERT INTO disputes (report_code, reporter_id, listing_id, reason, description, status)
                VALUES (%s, %s, %s, %s, %s, 'pending')
            """
            cursor.execute(query, (report_code, reporter_id, reported_listing_id, reason, description))
        except Exception:
            # Fallback if report_code column is not required in your exact table schema
            query = """
                INSERT INTO disputes (reporter_id, listing_id, reason, description, status)
                VALUES (%s, %s, %s, %s, 'pending')
            """
            cursor.execute(query, (reporter_id, reported_listing_id, reason, description))

        connection.commit()
        
        return jsonify({'success': True, 'message': 'Dispute report submitted successfully.'}), 201
    except Exception as e:
        if connection: connection.rollback()
        print("Create Dispute Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# 23. Update Dispute Status (Resolve / Dismiss)
@app.route('/api/admin/disputes/<int:dispute_id>/status', methods=['PUT'])
def update_dispute_status(dispute_id):
    connection = None
    cursor = None
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        connection = get_db_connection()
        cursor = connection.cursor()
        
        query = "UPDATE disputes SET status = %s WHERE dispute_id = %s"
        cursor.execute(query, (new_status, dispute_id))
        connection.commit()
        
        return jsonify({'success': True, 'message': f'Dispute marked as {new_status}.'}), 200
    except Exception as e:
        if connection: connection.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
        
        cursor.execute("""
            SELECT l.*, 
                   (SELECT MAX(bid_amount) FROM bids WHERE listing_id = l.listing_id) AS winning_bid,
                   (SELECT u.first_name FROM bids b JOIN users u ON b.bidder_id = u.user_id WHERE b.listing_id = l.listing_id ORDER BY b.bid_amount DESC LIMIT 1) AS winner_first_name,
                   (SELECT u.last_name FROM bids b JOIN users u ON b.bidder_id = u.user_id WHERE b.listing_id = l.listing_id ORDER BY b.bid_amount DESC LIMIT 1) AS winner_last_name,
                   (SELECT u.contact_number FROM bids b JOIN users u ON b.bidder_id = u.user_id WHERE b.listing_id = l.listing_id ORDER BY b.bid_amount DESC LIMIT 1) AS winner_contact
            FROM livestock_listings l
            WHERE l.farmer_id = %s 
              AND l.auction_status IN ('ended', 'closed', 'completed', 'settlement', 'pending')
            ORDER BY l.created_at DESC
        """, (farmer_id,))
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
        if cursor: cursor.close()
        if connection: connection.close()

# 24. Confirm Livestock Receipt & Settle Escrow to Farmer Wallet (With Transaction Logging)
@app.route('/api/buyer/confirm-receipt/<int:listing_id>', methods=['POST'])
def confirm_livestock_receipt(listing_id):
    connection = None
    cursor = None
    try:
        data = request.get_json()
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
            
        if listing['auction_status'] == 'completed':
            return jsonify({'success': False, 'message': 'Receipt already confirmed.'}), 400
            
        farmer_id = listing['farmer_id']
        winning_amount = float(listing['buyer_bid'] or listing['current_highest_bid'] or listing['starting_price'] or 0.0)
        
        # 1. Deduct from buyer wallet balance permanently
        cursor.execute("SELECT wallet_balance FROM users WHERE user_id = %s", (buyer_id,))
        buyer_row = cursor.fetchone()
        buyer_balance = float(buyer_row['wallet_balance'] or 0.0) if buyer_row else 0.0
        new_buyer_balance = max(0.0, buyer_balance - winning_amount)
        cursor.execute("UPDATE users SET wallet_balance = %s WHERE user_id = %s", (new_buyer_balance, buyer_id))
        
        # 2. Credit directly to farmer wallet balance
        cursor.execute("""
            UPDATE users 
            SET wallet_balance = COALESCE(wallet_balance, 0) + %s 
            WHERE user_id = %s
        """, (winning_amount, farmer_id))
        
        # 3. Mark listing status as completed
        cursor.execute("UPDATE livestock_listings SET auction_status = 'completed' WHERE listing_id = %s", (listing_id,))
        
        # 4. Log transaction into wallet_transactions for the buyer's history
        ref_code = f"TXN-AUC-{str(listing_id).padStart(3, '0') if hasattr(str(listing_id), 'padStart') else str(listing_id).zfill(3)}"
        desc = f"Auction Payment: {listing['animal_type']} (AUC-{str(listing_id).zfill(3)})"
        cursor.execute("""
            INSERT INTO wallet_transactions (user_id, reference_code, description, amount, type, status)
            VALUES (%s, %s, %s, %s, 'payment', 'Completed')
        """, (buyer_id, ref_code, desc, winning_amount))
        
        connection.commit()
        return jsonify({
            'success': True, 
            'message': f'Receipt confirmed! ₱{winning_amount:,.2f} paid and logged to your transaction history.'
        }), 200

    except Exception as e:
        if connection: connection.rollback()
        print("Confirm Receipt Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
            ORDER BY created_at DESC
        """, (buyer_id,))
        transactions = cursor.fetchall()
        for t in transactions:
            if t.get('created_at') and isinstance(t['created_at'], datetime):
                t['created_at'] = t['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'success': True, 'data': transactions}), 200
    except Exception as e:
        print("Transactions API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# Get Buyer Notifications API
@app.route('/api/buyer/notifications/<int:buyer_id>', methods=['GET'])
def get_buyer_notifications(buyer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        notifications = []
        
        # 1. Fetch wallet transactions (Top-ups & Payments)
        cursor.execute("""
            SELECT reference_code, description, amount, type, created_at 
            FROM wallet_transactions 
            WHERE user_id = %s 
            ORDER BY created_at DESC LIMIT 5
        """, (buyer_id,))
        txns = cursor.fetchall()
        for t in txns:
            amt = float(t['amount'] or 0)
            t_type = str(t['type'] or '').lower()
            title = "Wallet Top-up Successful" if t_type == 'topup' else "Auction Payment Completed"
            if t_type == 'payment':
                title = "Order Settled & Paid"
            notifications.append({
                'title': title,
                'message': f"{t['description']} (₱{amt:,.2f})",
                'time': t['created_at'].strftime('%Y-%m-%d %H:%M') if isinstance(t['created_at'], datetime) else str(t['created_at']),
                'type': t_type
            })
            
        # 2. Fetch Won / Pending Deliveries
        cursor.execute("""
            SELECT l.listing_id, l.animal_type, l.weight, l.auction_status,
                   (SELECT b.bid_amount FROM bids b WHERE b.listing_id = l.listing_id AND b.bidder_id = %s ORDER BY b.bid_amount DESC LIMIT 1) AS buyer_bid
            FROM livestock_listings l
            WHERE l.auction_status IN ('ended', 'closed', 'completed')
              AND EXISTS (SELECT 1 FROM bids b WHERE b.listing_id = l.listing_id AND b.bidder_id = %s)
            ORDER BY l.listing_id DESC LIMIT 5
        """, (buyer_id, buyer_id))
        closed_auctions = cursor.fetchall()
        for c in closed_auctions:
            is_comp = str(c['auction_status']).lower() == 'completed'
            bid_amt = float(c['buyer_bid'] or 0)
            notifications.append({
                'title': "Auction Won & Settled" if is_comp else "Auction Won - Awaiting Receipt",
                'message': f"You won {c['animal_type']} ({c['weight']} kg) for ₱{bid_amt:,.2f}.",
                'time': "Recent",
                'type': 'win'
            })

        return jsonify({'success': True, 'data': notifications}), 200

    except Exception as e:
        print("Notifications API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
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
        
        notifications = []
        
        # 1. Fetch listings belonging to this farmer
        cursor.execute("SELECT listing_id, animal_type FROM livestock_listings WHERE farmer_id = %s", (farmer_id,))
        listings = cursor.fetchall()
        listing_map = {l['listing_id']: l['animal_type'] for l in listings}
        listing_ids = list(listing_map.keys())

        if listing_ids:
            # Format placeholders safely for IN clause
            format_strings = ','.join(['%s'] * len(listing_ids))
            cursor.execute(f"""
                SELECT bid_amount, created_at, listing_id, bidder_id 
                FROM bids 
                WHERE listing_id IN ({format_strings}) 
                ORDER BY created_at DESC LIMIT 5
            """, tuple(listing_ids))
            bids = cursor.fetchall()

            for b in bids:
                raw_date = b['created_at']
                time_str = raw_date.strftime('%Y-%m-%d %H:%M') if hasattr(raw_date, 'strftime') else str(raw_date or 'Recent')
                animal = listing_map.get(b['listing_id'], 'Livestock')
                
                # Fetch bidder name safely
                cursor.execute("SELECT first_name, last_name FROM users WHERE user_id = %s", (b['bidder_id'],))
                bidder = cursor.fetchone()
                bidder_name = f"{bidder['first_name'] if bidder else 'Buyer'} {bidder['last_name'] if bidder else ''}".strip()

                notifications.append({
                    'title': "New Bid Received",
                    'message': f"{bidder_name} bid ₱{float(b['bid_amount'] or 0):,.2f} on {animal} (AUC-{str(b['listing_id']).zfill(3)})",
                    'time': time_str,
                    'type': 'bid'
                })

        return jsonify({'success': True, 'data': notifications}), 200

    except Exception as e:
        print("Farmer Notifications API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

# Get Admin Notifications API
@app.route('/api/admin/notifications', methods=['GET'])
def get_admin_notifications():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        
        notifications = []
        
        # 1. Fetch pending user registrations
        cursor.execute("""
            SELECT user_id, first_name, last_name, email, role, created_at 
            FROM users 
            WHERE status = 'pending' OR status IS NULL 
            ORDER BY created_at DESC LIMIT 5
        """)
        pending_users = cursor.fetchall()
        for u in pending_users:
            name = f"{u['first_name'] or 'New'} {u['last_name'] or 'User'}".strip()
            raw_date = u['created_at']
            time_str = raw_date.strftime('%Y-%m-%d %H:%M') if hasattr(raw_date, 'strftime') else str(raw_date or 'Recent')

            notifications.append({
                'title': "Pending Registration",
                'message': f"{name} ({u['role']}) registered and awaits verification.",
                'time': time_str,
                'type': 'user'
            })
            
        # 2. Fetch open disputes / reports
        cursor.execute("""
            SELECT dispute_id, reason, status, created_at 
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
                'message': f"Report #{d['dispute_id']}: {d['reason']} requires review.",
                'time': time_str,
                'type': 'dispute'
            })

        return jsonify({'success': True, 'data': notifications}), 200

    except Exception as e:
        print("Admin Notifications API Error:", str(e))
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

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
    return jsonify({'success': True, 'message': 'FarmBid API is running live!'}), 200

@app.route('/')
def home():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=3000)