# Multi-stage build: libloot wheel + runtime
# Stage 1: Build libloot Python bindings
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential pkg-config libssl-dev git \
    && rm -rf /var/lib/apt/lists/*

# Install Rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install maturin
RUN pip install --no-cache-dir maturin

# Build libloot wheel
RUN git clone --depth 1 https://github.com/loot/libloot.git /tmp/libloot \
    && cd /tmp/libloot/python \
    && maturin build --release --strip \
    && cp /tmp/libloot/python/target/wheels/*.whl /tmp/libloot.whl \
    || echo "libloot build failed — continuing without LOOT support"

# Stage 2: Runtime
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    unrar-free \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install libloot wheel if it was built
COPY --from=builder /tmp/libloot.whl* /tmp/
RUN pip install --no-cache-dir /tmp/libloot.whl 2>/dev/null || true && rm -f /tmp/libloot.whl

# Install the tool
COPY . .
RUN pip install --no-cache-dir -e .

# Pre-download LOOT masterlists for common games
RUN mkdir -p /root/.cache/nexus-dl/masterlists && \
    for game in starfield skyrimse fallout4 skyrim; do \
        curl -sSfL "https://raw.githubusercontent.com/loot/${game}/v0.21/masterlist.yaml" \
            -o "/root/.cache/nexus-dl/masterlists/${game}.yaml" 2>/dev/null || \
        curl -sSfL "https://raw.githubusercontent.com/loot/${game}/master/masterlist.yaml" \
            -o "/root/.cache/nexus-dl/masterlists/${game}.yaml" 2>/dev/null || true; \
    done

ENTRYPOINT ["nexus-dl"]
