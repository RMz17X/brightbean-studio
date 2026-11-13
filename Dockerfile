FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for Tailwind build
# LOCAL PATCH (2026-11-12): guarantee `npm` exists after the install.
# Upstream installs only `nodejs`, which bundles npm when the NodeSource
# repo is reachable — but when it is not, apt falls back to Debian's
# `nodejs`, which splits npm into a separate package, and the Tailwind
# build then fails with "npm: not found" (hit 2026-11-12). NOTE: do NOT
# just add `npm` to the install list — NodeSource's `nodejs` package
# Conflicts with Debian's `npm` and apt then refuses both (hit 2026-11-12
# on the first attempt at this patch). Install Debian's npm only when the
# bundled one is missing.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && (command -v npm || apt-get install -y npm) \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build Tailwind CSS
RUN cd theme/static_src && npm ci && npm run build

# Collect static files
RUN DJANGO_SETTINGS_MODULE=config.settings.production \
    SECRET_KEY=build-placeholder \
    DATABASE_URL=sqlite:///tmp/build.db \
    python manage.py collectstatic --noinput

EXPOSE 8000

# One worker, threaded. Importing this app costs ~100 MB before serving a
# single request, so two workers alone exceeded a 512 MB dyno's quota and the
# whole thing ran in swap. Raise --workers only alongside the container memory
# to match (budget ~250 MB per worker).
#
# Deliberately NO --max-requests. gthread sets ``alive = False`` at the START of
# the request that trips the counter, which stops the heartbeat while that
# request is still running — the arbiter then SIGKILLs the worker after
# ``timeout`` (30s) and the client gets a dropped connection. Verified against
# gunicorn 22: a 12s request that tripped --max-requests was killed, while the
# same request without it returned 200. Uploads here are allowed up to 1 GB, so
# no fixed timeout makes recycling safe. The RSS ratchet it was guarding against
# is fixed at the source instead (AWS_S3_MAX_MEMORY_SIZE + streaming reads).
CMD gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4
