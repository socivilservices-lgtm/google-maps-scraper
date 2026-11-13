# Playwright's Python image ships the OS libraries Chromium needs.
# Rebuild browsers after pip so they match the installed Playwright package.
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.49.1-jammy
FROM ${PLAYWRIGHT_IMAGE}

USER root
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    GMAPS_HEADLESS=true \
    GMAPS_CHROMIUM_NO_SANDBOX=true \
    GMAPS_OUTPUT_DIR=/data \
    GMAPS_USER_DATA_DIR=/data/profile

COPY pyproject.toml README.md requirements.txt ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && mkdir -p /data

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "gmaps_service"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
