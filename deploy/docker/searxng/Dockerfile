FROM ghcr.io/searxng/searxng:2026.11.6-ac11e14c5@sha256:c750ee1d859b26d3aeae51c07f6a48c3a0bfbaa9195d71a18b258862d88c4c3c
COPY --chown=977:977 settings.yml /usr/local/searxng/catsbot-settings.yml
ENV SEARXNG_SETTINGS_PATH=/usr/local/searxng/catsbot-settings.yml
# This private API has no user sessions; its signing secret can rotate on restart.
ENTRYPOINT ["/bin/sh", "-c", "export SEARXNG_SECRET=\"$(/usr/local/searxng/.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')\"; exec /usr/local/searxng/entrypoint.sh"]
