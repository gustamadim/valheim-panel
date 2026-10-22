FROM python:3.12-slim

# No pip installs: the panel is stdlib-only on purpose. This host's egress proxy
# makes PyPI unreliable, and a zero-dependency image keeps rebuilds reproducible.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PANEL_ENV=/app/panel.env

WORKDIR /app

# Copy only what runs. Tests and deploy scripts stay out of the image.
COPY panel.py /app/panel.py
COPY core/ /app/core/
COPY static/ /app/static/

# Run unprivileged. The panel needs no capabilities: it talks to supervisor and
# RCON over loopback TCP and reads /config, nothing more.
#
# Ownership is world-readable on purpose: docker-compose pins the runtime uid to
# the game container's PUID (1000) so files under /config keep consistent
# ownership. Baking a fixed uid here would break that, so the image stays
# uid-agnostic and only guarantees read/execute access.
RUN useradd --system --uid 10001 --no-create-home panel \
    && chown -R root:root /app \
    && chmod -R a+rX /app \
    && chmod 0755 /app
USER panel

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/healthz',timeout=4).status==200 else 1)"

CMD ["python3", "/app/panel.py"]
