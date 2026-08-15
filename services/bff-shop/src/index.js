require('dotenv').config();
const express = require('express');
const cors = require('cors');
const logger = require('./utils/logger');
const shopRouter = require('./routes/shop');

const app = express();
const PORT = process.env.PORT || 8001;

app.use(cors());
app.use(express.json());

// Routes
app.use('/api/shop', shopRouter);

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

// Global Error Handler
app.use((err, req, res, next) => {
  logger.error(`Unhandled exception: ${err.message}`, { stack: err.stack });
  res.status(500).json({ error: 'Internal Server Error' });
});

app.listen(PORT, () => {
  logger.info(`BFF Shop Service listening on port ${PORT}`);
});
