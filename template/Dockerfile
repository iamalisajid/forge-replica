# Replica image: builds the SPA and the API, then serves both on port 3000.
# The same image runs the one-shot `migrate` service (prisma migrate deploy).
#
# BASE_IMAGE defaults to node:22-alpine, and the build downloads its dependencies. forge builds offline
# (`network: none`) by passing its template base image (forge-template-base:<hash>), which already holds
# openssl, node_modules for this exact lockfile and the Prisma engines, so every download step is skipped.
ARG BASE_IMAGE=node:22-alpine
FROM ${BASE_IMAGE}
RUN command -v openssl >/dev/null 2>&1 || apk add --no-cache openssl
ENV CHECKPOINT_DISABLE=1 PRISMA_HIDE_UPDATE_MESSAGE=1
WORKDIR /app
COPY package.json package-lock.json ./
RUN if [ ! -d node_modules ]; then npm ci --no-audit --no-fund; fi
COPY . .
RUN npx prisma generate --schema db/schema.prisma && npm run build
ENV NODE_ENV=production PORT=3000
EXPOSE 3000
CMD ["node", "api/dist/server.js"]
