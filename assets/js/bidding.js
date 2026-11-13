/* ==========================================================================
   CENTRAL BIDDING LOGIC (assets/js/bidding.js)
   ========================================================================== */

async function placeBid(listingId, bidderId, bidAmount) {
  try {
    const response = await fetch('http://127.0.0.1:5000/api/bids', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ listingId, bidderId, bidAmount })
    });
    const data = await response.json();
    return data;
  } catch (err) {
    console.error('Error placing bid:', err);
    return { success: false, message: 'Network error connecting to FarmBid server.' };
  }
}

async function fetchListingBids(listingId) {
  try {
    const response = await fetch(`http://127.0.0.1:5000/api/bids/listing/${listingId}`);
    const data = await response.json();
    return data.success ? data.data : [];
  } catch (err) {
    console.error('Error fetching bids:', err);
    return [];
  }
}