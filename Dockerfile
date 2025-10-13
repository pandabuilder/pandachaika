# Stage 1: Base build stage
FROM python:3.14 AS builder

# Create the app directory
RUN mkdir /app

# Set the working directory
WORKDIR /app

# Set environment variables to optimize Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG DEBIAN_FRONTEND=noninteractive
RUN apt update && apt install -y libpq-dev

# Upgrade pip and install dependencies
RUN pip install uv

# Copy the requirements file first (better caching)
COPY requirements.txt /app/

# Install Python dependencies
RUN uv pip install --system --no-cache-dir -r requirements.txt psycopg[c]

# Stage 2: Production stage
FROM python:3.14-slim

RUN apt update && apt install -y libpq5 gosu

RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g 1000 -m appuser && \
    mkdir /app && \
    chown -R appuser /app

# Copy the Python dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Set the working directory
WORKDIR /app

# Copy application code
COPY --chown=appuser:appuser . .
COPY ./dockerentry.sh /dockerentry.sh
RUN chmod +x /dockerentry.sh

VOLUME /config/
VOLUME /media/
VOLUME /static/

# Set environment variables to optimize Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Expose the application port
EXPOSE 8090

LABEL org.opencontainers.image.description="PandaChaika Backend"

# Start the application
ENTRYPOINT ["/bin/bash", "/dockerentry.sh"]