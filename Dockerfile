FROM node:22-slim

# Install Bob Shell
RUN npm install -g bobshell

# Install Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY server.py .

ENV HOST=0.0.0.0
ENV PORT=8787
ENV BOB_BIN=bob
ENV MAX_CONCURRENT=4
ENV BOB_TIMEOUT=120

EXPOSE 8787

# Accept Bob Shell license on first run
RUN bob --accept-license --auth-method api-key -p "hello" || true

CMD ["python3", "server.py"]
