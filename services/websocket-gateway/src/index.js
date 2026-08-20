require('dotenv').config();
const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const jwt = require('jsonwebtoken');
const { createRedisClient, describeConnection } = require('node-common/redis_client');
const logger = require('./utils/logger');
const cors = require('cors');

const PORT = process.env.PORT || 8003;
const JWT_SECRET = process.env.JWT_SECRET || 'super-secret-jwt-key';

const app = express();
app.use(cors());

// Basic health check for container orchestration
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

const server = http.createServer(app);

const io = new Server(server, {
  cors: {
    origin: '*', // In production, restrict this to specific origins
    methods: ['GET', 'POST']
  }
});

// Authentication Middleware for Socket.io
io.use((socket, next) => {
  const token = socket.handshake.auth.token;
  if (!token) {
    logger.warn(`Socket connection rejected: No token provided (Socket ID: ${socket.id})`);
    return next(new Error('Authentication error: Token required'));
  }

  try {
    const decoded = jwt.verify(token, JWT_SECRET);
    // Attach user details to socket for later use
    socket.user = decoded;
    next();
  } catch (err) {
    logger.warn(`Socket connection rejected: Invalid token (Socket ID: ${socket.id})`);
    return next(new Error('Authentication error: Invalid token'));
  }
});

// Pub/Sub clients, sentinel-aware like every other Redis consumer.
//
// Two separate connections, not one shared: a connection in subscriber mode
// cannot issue ordinary commands, so publishing over the subscriber would
// fail. Both follow the primary -- a subscriber attached to a replica would
// keep receiving until the moment of a failover and then go quiet, which
// looks like "no events happening" rather than like a fault.
const pubClient = createRedisClient(process.env);
const subClient = createRedisClient(process.env);

subClient.on('error', (err) => {
  logger.error(`Redis Subscriber Error: ${err.message}`);
});
pubClient.on('error', (err) => {
  logger.error(`Redis Publisher Error: ${err.message}`);
});

// Subscribe to relevant backend channels
const channels = ['order-updates', 'fulfillment-updates', 'notification-updates'];
subClient.subscribe(...channels, (err, count) => {
  if (err) {
    logger.error(`Failed to subscribe to Redis channels: ${err.message}`);
  } else {
    logger.info(`Subscribed to ${count} Redis channels: ${channels.join(', ')} (${describeConnection(process.env)})`);
  }
});

// Handle incoming messages from Redis and broadcast to specific user rooms
subClient.on('message', (channel, message) => {
  logger.info(`Received message from Redis channel: ${channel}`);
  try {
    const parsedMessage = JSON.parse(message);
    // Assuming backend services include user_id in the message payload for targeted delivery
    const userId = parsedMessage.user_id;

    if (userId) {
      // Emit to the specific user's room
      io.to(`user_${userId}`).emit(channel, parsedMessage);
      logger.info(`Broadcasted message on channel ${channel} to room user_${userId}`);
    } else {
      // Broadcast to all if no specific user is targeted (e.g., system-wide announcements)
      io.emit(channel, parsedMessage);
      logger.info(`Broadcasted message on channel ${channel} to all connected clients`);
    }
  } catch (err) {
    logger.error(`Failed to parse or broadcast Redis message: ${err.message}`);
  }
});

// Handle Socket Connections
io.on('connection', (socket) => {
  const userId = socket.user.sub || socket.user.user_id;
  logger.info(`Client connected: Socket ID ${socket.id}, User ID: ${userId}`);

  // Join a dedicated room for this user to receive targeted pushes
  const roomName = `user_${userId}`;
  socket.join(roomName);
  logger.info(`Socket ${socket.id} joined room ${roomName}`);

  socket.on('disconnect', (reason) => {
    logger.info(`Client disconnected: Socket ID ${socket.id}, Reason: ${reason}`);
  });

  socket.on('error', (err) => {
    logger.error(`Socket error for ${socket.id}: ${err.message}`);
  });
});

server.listen(PORT, () => {
  logger.info(`Websocket Gateway Service listening on port ${PORT}`);
});
