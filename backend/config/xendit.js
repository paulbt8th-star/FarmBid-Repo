/* ==========================================================================
   XENDIT LIVE API CONFIGURATION (backend/config/xendit.js)
   ========================================================================== */
const { Xendit } = require('xendit-node');

// Initialize Xendit with your Live Secret Key from environment variables
const xenditApiKey = process.env.XENDIT_SECRET_KEY || 'xnd_production_your_live_key_here';

const xendit = new Xendit({
    secretKey: xenditApiKey,
});

// Extract Invoice and Payout services from the initialized Xendit instance
const invoiceClient = xendit.Invoice;
const payoutClient = xendit.Payout;
const balanceClient = xendit.Balance;

/**
 * Helper function to create a Xendit Invoice for livestock escrow payment
 * @param {Object} orderData - Contains external_id, amount, description, customer info
 */
async function createEscrowInvoice(orderData) {
    try {
        const response = await invoiceClient.createInvoice({
            data: {
                externalId: orderData.externalId,
                amount: Number(orderData.amount),
                description: orderData.description,
                invoiceDuration: 86400, // 24 hours settlement window
                currency: 'PHP',
                customer: {
                    givenNames: orderData.buyerName,
                    email: orderData.buyerEmail,
                    mobileNumber: orderData.buyerPhone || '+639123456789',
                },
                successRedirectUrl: `${process.env.CLIENT_URL || 'http://localhost:3000'}/bidder/my-purchases.html`,
                failureRedirectUrl: `${process.env.CLIENT_URL || 'http://localhost:3000'}/bidder/wallet.html`,
            },
        });
        return { success: true, data: response };
    } catch (error) {
        console.error('❌ Xendit Invoice Creation Error:', error);
        return { success: false, error: error.message };
    }
}

module.exports = {
    xendit,
    invoiceClient,
    payoutClient,
    balanceClient,
    createEscrowInvoice
};