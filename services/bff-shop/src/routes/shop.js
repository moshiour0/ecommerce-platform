const express = require('express');
const { catalogClient, searchClient, cartClient } = require('../clients/downstream');
const logger = require('../utils/logger');

const router = express.Router();

// GET /api/shop/search
router.get('/search', async (req, res, next) => {
  try {
    logger.info(`BFF Search request: ${JSON.stringify(req.query)}`);
    const response = await searchClient.get('/search', { params: req.query });
    res.json(response.data);
  } catch (error) {
    logger.error(`Error in /search: ${error.message}`);
    if (error.response) {
      res.status(error.response.status).json(error.response.data);
    } else {
      next(error);
    }
  }
});

// GET /api/shop/products/:id
router.get('/products/:id', async (req, res, next) => {
  try {
    const { id } = req.params;
    logger.info(`BFF Get Product request for ID: ${id}`);
    const response = await catalogClient.get(`/products/${id}`);
    res.json(response.data);
  } catch (error) {
    logger.error(`Error in /products/:id: ${error.message}`);
    if (error.response) {
      res.status(error.response.status).json(error.response.data);
    } else {
      next(error);
    }
  }
});

// POST /api/shop/cart/:user_id/items
router.post('/cart/:user_id/items', async (req, res, next) => {
  try {
    const { user_id } = req.params;
    const idempotencyKey = req.headers['idempotency-key'];
    
    logger.info(`BFF Add to Cart request for user: ${user_id}`);

    const config = {
      headers: {}
    };
    
    if (idempotencyKey) {
      config.headers['Idempotency-Key'] = idempotencyKey;
    } else {
      return res.status(400).json({ detail: 'Idempotency-Key header is required' });
    }

    const response = await cartClient.post(`/cart/${user_id}/items`, req.body, config);
    res.status(response.status).json(response.data);
  } catch (error) {
    logger.error(`Error in /cart/:user_id/items: ${error.message}`);
    if (error.response) {
      res.status(error.response.status).json(error.response.data);
    } else {
      next(error);
    }
  }
});

module.exports = router;
