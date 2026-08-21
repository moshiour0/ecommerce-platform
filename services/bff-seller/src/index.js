require('dotenv').config();
const express = require('express');
const cors = require('cors');
const logger = require('./utils/logger');
const sellerRouter = require('./routes/seller');
const { breakerStates } = require('./clients/downstream');

const app = express();
const PORT = process.env.PORT || 8021;

app.use(cors());
app.use(express.json());

// Everything a seller can reach. Sellers must never call internal services
// directly (Rule 7): order-saga has an endpoint that marks a seller order
// delivered, which is correct for the courier integration and catastrophic
// for a seller, and this service is where that line is drawn.
app.use('/api/seller', sellerRouter);

app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

// Circuit state, so an operator can see which downstream is unhappy without
// reading logs.
app.get('/health/breakers', (req, res) => {
  res.json({ breakers: breakerStates() });
});

app.use((err, req, res, next) => {
  logger.error(`Unhandled exception: ${err.message}`, { stack: err.stack });
  res.status(500).json({ error: 'Internal Server Error' });
});

app.listen(PORT, () => {
  logger.info(`BFF Seller Service listening on port ${PORT}`);
});
