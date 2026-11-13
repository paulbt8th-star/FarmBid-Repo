/* ==========================================================================
   LISTING CONTROLLER (backend/controllers/listingController.js)
   ========================================================================== */
const fs = require('fs');
const path = require('path');
const db = require('../config/db');

/**
 * Helper to normalize single or multiple images stored in image_url
 */
function formatListingImages(row) {
  let images = [];
  if (row.image_url) {
    try {
      const parsed = JSON.parse(row.image_url);
      images = Array.isArray(parsed) ? parsed : [row.image_url];
    } catch (e) {
      images = [row.image_url];
    }
  } else {
    images = ['https://images.unsplash.com/photo-1541781774459-bb2af2f05b55'];
  }

  return {
    ...row,
    image_url: images[0], // Primary cover image for tables and thumbnails
    images: images        // Full array of images for the buyer gallery view
  };
}

/**
 * Get all marketplace livestock listings (for bidders and admin oversight)
 */
exports.getAllListings = async (req, res) => {
  try {
    const query = `
      SELECT l.*, u.first_name AS farmer_first_name, u.last_name AS farmer_last_name, u.address AS farmer_address
      FROM livestock_listings l
      JOIN users u ON l.farmer_id = u.user_id
      ORDER BY l.created_at DESC
    `;
    const [listings] = await db.execute(query);
    const formatted = listings.map(formatListingImages);
    return res.status(200).json({ success: true, count: formatted.length, data: formatted });
  } catch (error) {
    console.error('❌ Error fetching listings:', error);
    return res.status(500).json({ success: false, message: 'Server error while fetching listings.' });
  }
};

/**
 * Get listings belonging to a specific farmer (for farmer/manage-listings.html)
 */
exports.getListingsByFarmer = async (req, res) => {
  try {
    const { farmerId } = req.params;
    const query = `
      SELECT * FROM livestock_listings 
      WHERE farmer_id = ? 
      ORDER BY created_at DESC
    `;
    const [listings] = await db.execute(query, [farmerId]);
    const formatted = listings.map(formatListingImages);
    return res.status(200).json({ success: true, count: formatted.length, data: formatted });
  } catch (error) {
    console.error('❌ Error fetching farmer listings:', error);
    return res.status(500).json({ success: false, message: 'Server error while fetching farmer listings.' });
  }
};

/**
 * Create a new livestock listing with multi-photo support
 */
exports.createListing = async (req, res) => {
  try {
    const { farmerId, animalType, breed, sex, age, weight, startingPrice, auctionEndsAt, images, imageUrl } = req.body;

    // Ensure image_url column is TEXT so it can store multiple photo URLs
    try {
      await db.execute('ALTER TABLE livestock_listings MODIFY COLUMN image_url TEXT');
    } catch (alterErr) {
      // Ignore if already TEXT
    }

    const rawImages = Array.isArray(images) ? images : (imageUrl ? [imageUrl] : []);
    const uploadsDir = path.join(__dirname, '../../uploads');
    if (!fs.existsSync(uploadsDir)) {
      fs.mkdirSync(uploadsDir, { recursive: true });
    }

    const savedUrls = [];

    rawImages.forEach((imgStr, idx) => {
      if (imgStr && imgStr.startsWith('data:image/')) {
        const base64Data = imgStr.replace(/^data:image\/\w+;base64,/, '');
        const fileName = `livestock-${Date.now()}-${idx + 1}.jpg`;
        const filePath = path.join(uploadsDir, fileName);

        fs.writeFileSync(filePath, Buffer.from(base64Data, 'base64'));
        savedUrls.push(`http://127.0.0.1:5000/uploads/${fileName}`);
      } else if (imgStr) {
        savedUrls.push(imgStr);
      }
    });

    const finalImageValue = JSON.stringify(savedUrls);

    const query = `
      INSERT INTO livestock_listings 
      (farmer_id, animal_type, breed, sex, age, weight, starting_price, auction_status, auction_ends_at, image_url)
      VALUES (?, ?, ?, ?, ?, ?, ?, 'live', ?, ?)
    `;
    
    const [result] = await db.execute(query, [
      farmerId,
      animalType,
      breed,
      sex,
      age,
      weight,
      startingPrice,
      auctionEndsAt || null,
      finalImageValue
    ]);

    return res.status(201).json({
      success: true,
      message: 'Livestock listed successfully!',
      listingId: result.insertId,
      images: savedUrls
    });

  } catch (error) {
    console.error('❌ Error creating listing:', error);
    return res.status(500).json({ success: false, message: error.message || 'Server error while creating listing.' });
  }
};

/**
 * Update livestock auction status (e.g., draft, live, completed, flagged)
 */
exports.updateListingStatus = async (req, res) => {
  try {
    const { id } = req.params;
    const { status } = req.body;

    const query = `UPDATE livestock_listings SET auction_status = ? WHERE listing_id = ?`;
    await db.execute(query, [status, id]);

    return res.status(200).json({
      success: true,
      message: `Listing status updated to '${status}' successfully.`
    });
  } catch (error) {
    console.error('❌ Error updating listing status:', error);
    return res.status(500).json({ success: false, message: 'Server error while updating status.' });
  }
};