/* ==========================================================================
   BACKEND DATABASE CONFIGURATION (backend/config/db.js)
   ========================================================================== */
const mysql = require('mysql2');

// Create the connection pool to local MySQL (farmbid_db)
const pool = mysql.createPool({
    host: 'localhost',
    user: 'root',
    password: '', // Default XAMPP password is empty
    database: 'farmbid_db',
    waitForConnections: true,
    connectionLimit: 10,
    queueLimit: 0
});

// Test connection on startup
pool.getConnection((err, connection) => {
    if (err) {
        console.error('❌ Error connecting to FarmBid database:', err.message);
        if (err.code === 'PROTOCOL_CONNECTION_LOST') {
            console.error('Database connection was closed.');
        }
        if (err.code === 'ER_CON_COUNT_ERROR') {
            console.error('Database has too many connections.');
        }
        if (err.code === 'ECONNREFUSED') {
            console.error('Database connection was refused. Check if MySQL is running in XAMPP.');
        }
    } else {
        console.log('✅ Connected successfully to FarmBid MySQL database (farmbid_db)');
        connection.release();
    }
});

module.exports = pool.promise();