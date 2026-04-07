# Dockerfile — FFIS Flat File Scrubber
# Tuned for Fly.io: non-root user, .streamlit/config.toml baked in,
# secrets loaded from Fly secrets (not .env file).

FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN useradd -m -u 1000 appuser
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Streamlit configuration ───────────────────────────────────────────────────
RUN mkdir -p /app/.streamlit
RUN echo '\
[server]\n\
headless = true\n\
port = 8501\n\
address = "0.0.0.0"\n\
enableCORS = false\n\
enableXsrfProtection = false\n\
maxUploadSize = 500\n\
\n\
[browser]\n\
gatherUsageStats = false\n\
\n\
[theme]\n\
base = "dark"\n\
' > /app/.streamlit/config.toml

# ── Application source ────────────────────────────────────────────────────────
COPY --chown=appuser:appuser . .

# ── Switch to non-root ────────────────────────────────────────────────────────
USER appuser

# ── Health check ─────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "flat_file_scrubber.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
