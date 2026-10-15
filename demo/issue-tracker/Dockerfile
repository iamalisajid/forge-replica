FROM node:22-alpine
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY server.js ./
COPY public ./public
ENV PORT=4001 DB_PATH=/data/tracker.sqlite3 NODE_NO_WARNINGS=1
RUN mkdir -p /data
EXPOSE 4001
CMD ["node", "server.js"]
