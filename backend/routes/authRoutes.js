/* ==========================================================================
   AUTHENTICATION ROUTES (backend/routes/authRoutes.js)
   ========================================================================== */
const express = require('express');
const router = express.Router();
const authController = require('../controllers/authController');

// POST /api/auth/register - Register a new bidder or farmer
router.post('/register', authController.registerUser);

// POST /api/auth/login - Authenticate user login
router.post('/login', authController.loginUser);

module.exports = router;