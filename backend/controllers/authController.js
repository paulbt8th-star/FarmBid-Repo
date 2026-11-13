/* ==========================================================================
   AUTHENTICATION CONTROLLER (backend/controllers/authController.js)
   ========================================================================== */
const db = require('../config/db');
const bcrypt = require('bcryptjs');

/**
 * Handle User Registration (Bidder or Farmer)
 */
exports.registerUser = async (req, res) => {
    try {
        const { firstName, lastName, email, contactNumber, password, role, address } = req.body;

        // 1. Check if user already exists
        const [existingUsers] = await db.execute('SELECT user_id FROM users WHERE email = ?', [email]);
        if (existingUsers.length > 0) {
            return res.status(400).json({ success: false, message: 'Email is already registered.' });
        }

        // 2. Hash the password securely
        const salt = await bcrypt.genSalt(10);
        const passwordHash = await bcrypt.hash(password, salt);

        // 3. Determine initial account status (Farmers require admin approval per your settings workflow)
        const accountStatus = (role === 'farmer') ? 'pending' : 'active';

        // 4. Insert new user into database
        const query = `
      INSERT INTO users (first_name, last_name, email, contact_number, password_hash, role, address, account_status)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `;
        const [result] = await db.execute(query, [
            firstName,
            lastName,
            email,
            contactNumber,
            passwordHash,
            role,
            address || null,
            accountStatus
        ]);

        return res.status(201).json({
            success: true,
            message: 'Registration successful!',
            userId: result.insertId,
            accountStatus: accountStatus
        });

    } catch (error) {
        console.error('❌ Registration Error:', error);
        return res.status(500).json({ success: false, message: 'Server error during registration.' });
    }
};

/**
 * Handle User Login
 */
exports.loginUser = async (req, res) => {
    try {
        const { email, password } = req.body;

        // 1. Find user by email
        const [users] = await db.execute('SELECT * FROM users WHERE email = ?', [email]);
        if (users.length === 0) {
            return res.status(401).json({ success: false, message: 'Invalid email or password.' });
        }

        const user = users[0];

        // 2. Check if account is suspended or banned
        if (user.account_status === 'suspended' || user.account_status === 'banned') {
            return res.status(403).json({
                success: false,
                message: `Your account has been ${user.account_status}. Please contact admin support.`
            });
        }

        // 3. Compare password hash
        const isMatch = await bcrypt.compare(password, user.password_hash);
        if (!isMatch) {
            return res.status(401).json({ success: false, message: 'Invalid email or password.' });
        }

        // 4. Return success along with role for frontend routing
        return res.status(200).json({
            success: true,
            message: 'Login successful',
            user: {
                id: user.user_id,
                firstName: user.first_name,
                lastName: user.last_name,
                email: user.email,
                role: user.role,
                status: user.account_status
            }
        });

    } catch (error) {
        console.error('❌ Login Error:', error);
        return res.status(500).json({ success: false, message: 'Server error during login.' });
    }
};