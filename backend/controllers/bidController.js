/* ==========================================================================
   BID CONTROLLER (backend/controllers/bidController.js)
   ========================================================================== */
const db = require('../config/db');

/**
 * Place a new bid on a livestock listing
 */
exports.placeBid = async (req, res) => {
  try {
    const { listingId, bidderId, bidAmount } = req.body;

    // 1. Fetch the listing to verify it's live and check current highest price
    const [listings] = await db.execute(
      'SELECT * FROM livestock_listings WHERE listing_id = ?',
      [listingId]
    );

    if (listings.length === 0) {
      return res.status(404).json({ success: false, message: 'Livestock listing not found.' });
    }

    const listing = listings[0];

    if (listing.auction_status !== 'live') {
      return res.status(400).json({ success: false, message: 'This auction is no longer active.' });
    }

    // 2. Determine the minimum required bid
    const currentMin = listing.current_highest_bid || listing.starting_price;

    if (parseFloat(bidAmount) <= parseFloat(currentMin)) {
      return res.status(400).json({ 
        success: false, 
        message: `Bid must be higher than the current highest bid or starting price (₱${currentMin}).` 
      });
    }

    // 3. Insert the new bid into the bids table
    const insertBidQuery = `
      INSERT INTO bids (listing_id, bidder_id, bid_amount, is_leading)
      VALUES (?, ?, ?, TRUE)
    `;
    
    // First, set previous bids for this listing to is_leading = FALSE
    await db.execute('UPDATE bids SET is_leading = FALSE WHERE listing_id = ?', [listingId]);

    // Insert new bid
    const [result] = await db.execute(insertBidQuery, [listingId, bidderId, bidAmount]);

    // 4. Update the current_highest_bid on the livestock listing
    await db.execute(
      'UPDATE livestock_listings SET current_highest_bid = ? WHERE listing_id = ?',
      [bidAmount, listingId]
    );

    return res.status(201).json({
      success: true,
      message: 'Bid placed successfully!',
      bidId: result.insertId,
      newHighestBid: bidAmount
    });

  } catch (error) {
    console.error('❌ Error placing bid:', error);
    return res.status(500).json({ success: false, message: 'Server error while placing bid.' });
  }
};

/**
 * Get all bids for a specific livestock listing
 */
exports.getBidsByListing = async (req, res) => {
  try {
    const { listingId } = req.params;
    const query = `
      SELECT b.*, u.first_name, u.last_name 
      FROM bids b
      JOIN users u ON b.bidder_id = u.user_id
      WHERE b.listing_id = ?
      ORDER BY b.bid_amount DESC
    `;
    const [bids] = await db.execute(query, [listingId]);
    return res.status(200).json({ success: true, count: bids.length, data: bids });
  } catch (error) {
    console.error('❌ Error fetching bids:', error);
    return res.status(500).json({ success: false, message: 'Server error while fetching bids.' });
  }
};