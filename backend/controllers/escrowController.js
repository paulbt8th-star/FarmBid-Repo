/* ==========================================================================
   ESCROW CONTROLLER (backend/controllers/escrowController.js)
   ========================================================================== */
const db = require('../config/db');
const { createEscrowInvoice } = require('../config/xendit');

/**
 * Create a Xendit Invoice for secure livestock escrow checkout
 */
exports.createCheckoutInvoice = async (req, res) => {
  try {
    const { listingId, buyerId, amount, buyerName, buyerEmail, buyerPhone } = req.body;

    // Generate unique external ID for Xendit
    const externalId = `farmbid-escrow-${listingId}-${Date.now()}`;

    // 1. Request Xendit invoice creation via helper
    const xenditResult = await createEscrowInvoice({
      externalId,
      amount: parseFloat(amount),
      description: `FarmBid Secure Escrow Payment for Livestock Listing #${listingId}`,
      buyerName,
      buyerEmail,
      buyerPhone
    });

    if (!xenditResult.success) {
      return res.status(400).json({ 
        success: false, 
        message: 'Failed to generate Xendit payment invoice: ' + xenditResult.error 
      });
    }

    const invoiceData = xenditResult.data;

    // 2. Record pending transaction in escrow_transactions table
    const query = `
      INSERT INTO escrow_transactions 
      (listing_id, buyer_id, amount, xendit_invoice_id, status, payment_url)
      VALUES (?, ?, ?, ?, 'pending', ?)
    `;
    
    await db.execute(query, [
      listingId,
      buyerId,
      amount,
      invoiceData.id,
      invoiceData.invoiceUrl
    ]);

    return res.status(201).json({
      success: true,
      message: 'Escrow payment invoice generated successfully.',
      invoiceUrl: invoiceData.invoiceUrl,
      invoiceId: invoiceData.id,
      externalId: externalId
    });

  } catch (error) {
    console.error('❌ Error creating escrow invoice:', error);
    return res.status(500).json({ success: false, message: 'Server error during escrow initialization.' });
  }
};