/* ==========================================================================
   BID ROUTES (backend/routes/bidRoutes.js)
   ========================================================================== */
const express = require('express');
const router = express.Router();
const bidController = require('../controllers/bidController');

// POST /api/bids - Place a new bid
router.post('/', bidController.placeBid);

// GET /api/bids/listing/:listingId - Get all bids for a specific listing
router.get('/listing/:listingId', bidController.getBidsByListing);

module.exports = router;