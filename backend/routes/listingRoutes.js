/* ==========================================================================
   LISTING ROUTES (backend/routes/listingRoutes.js)
   ========================================================================== */
const express = require('express');
const router = express.Router();
const listingController = require('../controllers/listingController');

// GET /api/listings - Get all marketplace listings
router.get('/', listingController.getAllListings);

// GET /api/listings/farmer/:farmerId - Get listings for a specific farmer
router.get('/farmer/:farmerId', listingController.getListingsByFarmer);

// POST /api/listings - Create a new livestock listing
router.post('/', listingController.createListing);

// PUT /api/listings/:id/status - Update listing status
router.put('/:id/status', listingController.updateListingStatus);

module.exports = router;