/* ==========================================================================
   XENDIT ESCROW CHECKOUT (assets/js/xendit-checkout.js)
   ========================================================================== */

async function createXenditEscrowInvoice(amount, buyerId, listingId) {
  try {
    const response = await fetch('http://127.0.0.1:5000/api/escrow/create-invoice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount, buyerId, listingId })
    });
    const data = await response.json();
    return data;
  } catch (err) {
    console.error('Error creating Xendit invoice:', err);
    return { success: false, message: 'Failed to initiate Xendit payment.' };
  }
}