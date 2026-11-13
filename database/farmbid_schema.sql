-- ==========================================================================
-- FARMBID DATABASE SCHEMA (database/farmbid_schema.sql)
-- ==========================================================================

CREATE DATABASE IF NOT EXISTS farmbid_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE farmbid_db;

-- 1. USERS TABLE 
-- Supports Authentication, Role-Based Access Control (Bidder, Farmer, Admin), and Admin User Approvals
CREATE TABLE IF NOT EXISTS users (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    contact_number VARCHAR(20) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('bidder', 'farmer', 'admin') NOT NULL,
    address VARCHAR(255) DEFAULT NULL, -- Primarily utilized by farmers during registration
    account_status ENUM('pending', 'active', 'suspended', 'banned') DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. LIVESTOCK LISTINGS TABLE 
-- Supports Farmer Inventory Management, Marketplace Catalog, and Auction Timers
CREATE TABLE IF NOT EXISTS livestock_listings (
    listing_id INT AUTO_INCREMENT PRIMARY KEY,
    farmer_id INT NOT NULL,
    animal_type VARCHAR(50) NOT NULL,
    breed VARCHAR(100) NOT NULL,
    sex ENUM('Male', 'Female') NOT NULL,
    age VARCHAR(50) NOT NULL,
    weight DECIMAL(8,2) NOT NULL, -- in kg
    starting_price DECIMAL(12,2) NOT NULL,
    current_highest_bid DECIMAL(12,2) DEFAULT NULL,
    auction_status ENUM('draft', 'scheduled', 'live', 'completed', 'flagged', 'not_listed') DEFAULT 'draft',
    auction_ends_at DATETIME DEFAULT NULL,
    image_url VARCHAR(255) DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (farmer_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- 3. BIDS TABLE 
-- Supports Bidder Marketplace Catalog, Live Bidding Room, and Highest Bid Evaluation
CREATE TABLE IF NOT EXISTS bids (
    bid_id INT AUTO_INCREMENT PRIMARY KEY,
    listing_id INT NOT NULL,
    bidder_id INT NOT NULL,
    bid_amount DECIMAL(12,2) NOT NULL,
    is_leading BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (listing_id) REFERENCES livestock_listings(listing_id) ON DELETE CASCADE,
    FOREIGN KEY (bidder_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- 4. ESCROW TRANSACTIONS TABLE 
-- Supports Xendit Payment API Integration and Secure Fund Authorization
CREATE TABLE IF NOT EXISTS escrow_transactions (
    transaction_id INT AUTO_INCREMENT PRIMARY KEY,
    listing_id INT NOT NULL,
    buyer_id INT NOT NULL,
    farmer_id INT NOT NULL,
    amount DECIMAL(12,2) NOT NULL,
    xendit_invoice_id VARCHAR(100) DEFAULT NULL,
    escrow_status ENUM('awaiting_payment', 'paid_and_held', 'delivered_and_released', 'refunded') DEFAULT 'awaiting_payment',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (listing_id) REFERENCES livestock_listings(listing_id) ON DELETE CASCADE,
    FOREIGN KEY (buyer_id) REFERENCES users(user_id),
    FOREIGN KEY (farmer_id) REFERENCES users(user_id)
);

-- 5. DISPUTES TABLE 
-- Supports Admin Active Reports, Evidence Review Queue, and Case Extensions
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INT AUTO_INCREMENT PRIMARY KEY,
    report_code VARCHAR(20) UNIQUE NOT NULL, -- e.g., DSP-0261
    listing_id INT NOT NULL,
    reporter_id INT NOT NULL,
    reported_user_id INT NOT NULL,
    issue_title VARCHAR(150) NOT NULL,
    reporter_statement TEXT NOT NULL,
    priority ENUM('normal', 'high') DEFAULT 'normal',
    dispute_status ENUM('open', 'reviewing', 'resolved') DEFAULT 'open',
    assigned_admin_id INT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (listing_id) REFERENCES livestock_listings(listing_id),
    FOREIGN KEY (reporter_id) REFERENCES users(user_id),
    FOREIGN KEY (reported_user_id) REFERENCES users(user_id),
    FOREIGN KEY (assigned_admin_id) REFERENCES users(user_id)
);