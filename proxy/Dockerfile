# Password gate for non-public environments. See README.md.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
# Railway injects PORT; Caddy reads it in the Caddyfile (8080 when unset).
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
