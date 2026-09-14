FROM node:22-bookworm-slim AS build
WORKDIR /app
RUN npm install --global pnpm@10.32.1
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm typecheck && pnpm build

FROM node:22-bookworm-slim
RUN apt-get update && apt-get install --yes --no-install-recommends python3 python3-psycopg2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV NODE_ENV=production PORT=10000
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY public ./public
COPY collector ./collector
COPY scripts ./scripts
COPY package.json ./package.json
EXPOSE 10000
CMD ["node", "scripts/start-production.mjs"]
