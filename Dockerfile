# Production Dockerfile
FROM python:3.10-slim

LABEL maintainer="Bogdan199719"

WORKDIR /app

# Install system dependencies if needed (e.g., for building wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install dependencies
COPY pyproject.toml .
# Allow pip to install globally inside the container is standard usage for single-app containers
RUN pip install --no-cache-dir .

COPY . /app/project/

WORKDIR /app/project

CMD ["python3", "-m", "shop_bot"]