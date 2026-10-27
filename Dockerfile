# syntax=docker/dockerfile:1
#
# OpenHost packaging of Spliit (https://github.com/spliit-app/spliit).
#
# Everything runs in a single container:
#   - PostgreSQL (bundled, data on the OpenHost persistent volume)
#   - The Spliit Next.js app (standalone output)
#   - A tiny Python auth-proxy that terminates OpenHost SSO / owner-gating
#     and forwards to Next.js on loopback.
#
# ---------------------------------------------------------------------------
# Stage 1: build the Next.js standalone bundle
# ---------------------------------------------------------------------------
FROM node:26-alpine AS build

WORKDIR /usr/app

# Install only what's needed to resolve the dependency graph first so Docker
# can cache the (slow) npm install layer independently of source changes.
# Retried because the registry occasionally resets a connection mid-install
# (same loop as upstream's Dockerfile).
COPY package.json package-lock.json ./
RUN apk add --no-cache openssl && \
    for i in 1 2 3 4 5; do \
      npm ci --ignore-scripts --fetch-retries=5 --fetch-timeout=600000 && exit 0; \
      echo "npm ci failed (attempt $i of 5), retrying in 10s..."; \
      sleep 10; \
    done; \
    exit 1

# Copy the rest of the source. Prisma 7 generates the client into
# ./src/generated/prisma instead of node_modules, so generate after the
# source tree is in place.
COPY . .
RUN npx prisma generate

ENV NEXT_TELEMETRY_DISABLED=1
# Build-time env. These values are only used to satisfy `env.ts` validation
# during `next build`; the real connection string is injected at runtime.
COPY scripts/build.env .env
# Next copies .env into the standalone output; drop it so the mock build
# values never ship and a variable missing at runtime fails instead of
# silently resolving to a placeholder.
RUN npm run build \
    && rm -rf .next/cache \
    && rm -f .next/standalone/.env

# ---------------------------------------------------------------------------
# Stage 2: a self-contained Prisma CLI for `migrate deploy` at boot
# ---------------------------------------------------------------------------
# The standalone bundle does not include the Prisma CLI, and the build stage
# installs with --ignore-scripts (the repo's postinstall runs migrate deploy),
# so @prisma/engines never downloads the schema engine there. Install the
# same prisma version on its own, with scripts (mirrors upstream's Dockerfile).
FROM node:26-alpine AS prisma-cli

WORKDIR /opt/prisma-cli
ENV CHECKPOINT_DISABLE=1
RUN apk add --no-cache openssl
COPY --from=build /usr/app/node_modules/prisma/package.json ./_prisma.json
RUN PRISMA_VERSION="$(node -p "require('./_prisma.json').version")" && \
    rm -f ./_prisma.json && \
    npm init -y > /dev/null && \
    for i in 1 2 3 4 5; do \
      npm install --no-audit --no-fund --fetch-retries=5 \
        --fetch-timeout=600000 "prisma@${PRISMA_VERSION}" && exit 0; \
      echo "prisma install failed (attempt $i of 5), retrying in 10s..."; \
      sleep 10; \
    done; \
    exit 1

# ---------------------------------------------------------------------------
# Stage 3: runtime image
# ---------------------------------------------------------------------------
FROM node:26-alpine AS runtime

# System deps:
#   postgresql16 : bundled database (server + client + pg_ctl/pg_isready)
#   python3      : auth-proxy sidecar
#   openssl      : prisma schema engine (migrate deploy) runtime dep
#   su-exec      : drop privileges to the postgres user (alpine's gosu)
#   tini         : reap zombies for the long-lived supervisor
#   bash         : start.sh uses the `wait -n` bashism
RUN apk add --no-cache \
        postgresql16 \
        python3 \
        openssl \
        su-exec \
        tini \
        bash

WORKDIR /usr/app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV CHECKPOINT_DISABLE=1
# AWS SDK >= 3.729 adds a CRC32 checksum of the (empty) body to presigned
# upload URLs by default. The browser's PUT carries the real file, so the
# checksum never matches and S3-compatible providers that check it (or don't
# understand the parameter) reject the upload. Only send checksums when an
# operation requires one, which restores the pre-3.729 presigned URL shape for
# next-s3-upload's presigned uploads (expense documents / receipt scanning).
ENV AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED

# Next standalone output: the server + a minimal node_modules is emitted to
# .next/standalone; static assets live in .next/static and public/.
COPY --from=build /usr/app/.next/standalone ./
COPY --from=build /usr/app/.next/static ./.next/static
COPY --from=build /usr/app/public ./public

# Prisma migrations at runtime: the self-contained CLI from the prisma-cli
# stage, in a dedicated directory used ONLY by start.sh to apply migrations
# before the server starts (kept apart from the standalone node_modules).
# prisma.config.ts carries the datasource URL (Prisma 7 no longer reads it
# from schema.prisma); start.sh exports it.
COPY --from=prisma-cli /opt/prisma-cli/node_modules ./migrate/node_modules
COPY --from=build /usr/app/prisma ./migrate/prisma
COPY --from=build /usr/app/prisma.config.ts ./migrate/prisma.config.ts
COPY --from=build /usr/app/package.json ./migrate/package.json
COPY --from=build /usr/app/prisma ./prisma

# OpenHost integration layer.
COPY openhost/auth_proxy.py /usr/app/openhost/auth_proxy.py
COPY openhost/start.sh /usr/app/openhost/start.sh
RUN chmod 0755 /usr/app/openhost/start.sh /usr/app/openhost/auth_proxy.py

EXPOSE 8080/tcp

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/usr/app/openhost/start.sh"]
