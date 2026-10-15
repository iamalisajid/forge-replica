# Base image for every generated replica: pinned dependencies and Prisma engines, so checks run with --network none.
FROM node:22-alpine
RUN apk add --no-cache openssl
ENV CHECKPOINT_DISABLE=1 PRISMA_HIDE_UPDATE_MESSAGE=1 NODE_ENV=development
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY docker/bootstrap.prisma ./docker/bootstrap.prisma
RUN DATABASE_URL=postgresql://x:x@localhost:5432/x npx prisma generate --schema docker/bootstrap.prisma \
 && npx prisma --version
