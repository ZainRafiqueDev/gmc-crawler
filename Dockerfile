# Backend image for Render (or any Docker host). Playwright needs real
# Chromium + OS-level shared libraries that a plain "python:3.x-slim" image
# doesn't have - Playwright's own base image ships them so we don't have to
# hand-maintain an apt-get list that silently drifts out of date.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browser binaries are already baked into the base image (see FROM above);
# this just re-confirms/updates them against the installed playwright
# package version pinned in requirements.txt.
RUN playwright install --with-deps chromium

COPY . .

# Render sets $PORT at runtime; default to 8010 for local `docker run`.
ENV PORT=8010
EXPOSE 8010
CMD uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT}
