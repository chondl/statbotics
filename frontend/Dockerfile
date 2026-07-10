# Staging Cloud Run image for the statbotics Next.js 13 frontend, server mode
# (`next start`). Prod deploys to Vercel and ignores this file.
FROM node:20-slim

WORKDIR /app

# BACKEND_URL / BUCKET_URL / PROD are inlined into the bundle at `next build`
# time via next.config.js env block, so they must be present as build args.
ARG BACKEND_URL
ARG BUCKET_URL
ARG PROD=True
ENV BACKEND_URL=$BACKEND_URL \
    BUCKET_URL=$BUCKET_URL \
    PROD=$PROD

COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile

COPY . .

# next export fails upstream; build + `next start` is the supported path. The
# heap bump avoids OOM during `next build`.
RUN NODE_OPTIONS=--max_old_space_size=3072 yarn build

ENV NODE_ENV=production
# `next start` honors $PORT (Cloud Run injects 8080).
ENV PORT=8080
CMD ["yarn", "start"]
