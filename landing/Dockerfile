# Builds the static site, then serves it with Caddy. No Node in the final image.

# Dependencies only - also the image docker-compose.override.yml runs the dev
# server in, with the source bind-mounted over /app.
FROM node:26-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS build
COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
# Railway injects PORT; Caddy reads it in the Caddyfile (8080 when unset).
EXPOSE 8080
