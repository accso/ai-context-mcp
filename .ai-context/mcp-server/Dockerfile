# ── Build stage ───────────────────────────────────────────────────────────────
FROM node:22-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install --ignore-scripts
COPY tsconfig.json ./
COPY src/ ./src/
RUN npm run build

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM node:22-alpine
WORKDIR /app
COPY --from=builder /app/dist ./dist/
COPY --from=builder /app/node_modules ./node_modules/
COPY package.json .
ENV CONFIG_PATH=/app/config.yaml
EXPOSE 3000
CMD ["node", "dist/multi_server.js"]
