# Production Dockerfile
FROM python:3.10-slim

LABEL maintainer="Bogdan199719"

WORKDIR /app/project

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc docker.io git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy all files first so pip install . can find src/
COPY . .

# Install the package and its dependencies
RUN pip install --no-cache-dir .

CMD ["python3", "-m", "shop_bot"]