/* ==========================================================================
   ESCROW ROUTES (backend/routes/escrowRoutes.js)
   ========================================================================== */
const express = require('express');
const router = express.Router();
const escrowController = require('../controllers/escrowController');

// POST /api/escrow/create-invoice - Generate Xendit checkout invoice
router.post('/create-invoice', escrowController.createCheckoutInvoice);

module.exports = router;