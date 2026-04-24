# Production Dockerfile
FROM python:3.10-slim

LABEL maintainer="Bogdan199719"

WORKDIR /app/project

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc git \
    && rm -rf /var/lib/apt/lists/*

# Prefer IPv4 when a hostname has both A and AAAA records.
RUN printf 'precedence ::ffff:0:0/96  100\n' >> /etc/gai.conf

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy pyproject.toml first for dependency installation
COPY pyproject.toml README.md ./
COPY src ./src

# Install in editable mode so volume-mounted changes take effect
RUN pip install --no-cache-dir -e .

CMD ["python3", "-m", "shop_bot"]
