# syntax=docker/dockerfile:1.7

# Base image (builder)
FROM python:3.11-slim AS builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV TIMEZONE=America/New_York
ENV TZ=America/New_York

# Set work directory
WORKDIR /app

# Install build dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt constraints.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip -c constraints.txt && \
    pip install -r requirements.txt -c constraints.txt

# Base image (runtime)
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV TIMEZONE=America/New_York
ENV TZ=America/New_York

# Set work directory
WORKDIR /app

# Install runtime dependencies (match PostgreSQL server major version)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] http://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    clamav \
    clamav-daemon \
    clamdscan \
    libpq5 \
    postgresql-client-18 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
ARG APP_UID=1000
ARG APP_GID=1000
RUN addgroup --gid $APP_GID app \
    && adduser --uid $APP_UID --ingroup app --disabled-password --gecos "" --home /home/app app

# Copy installed Python packages
COPY --from=builder /usr/local /usr/local
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade --force-reinstall pip==26.1.2

# Copy project
COPY . .

# Install vendored business-deadline package when the optional local module is present.
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -d ./deadline_engine_module ]; then pip install --no-deps ./deadline_engine_module; fi

# Add entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app

USER app

# Expose port
EXPOSE 5000

# Run entrypoint
ENTRYPOINT ["/entrypoint.sh"]
