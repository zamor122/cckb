#!/usr/bin/env bash
set -euo pipefail

# Locate script directory and cd to it
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Starting Docker Compose stack..."
docker compose up -d

echo "Waiting for MinIO healthcheck to return 200..."
max_attempts=30
attempt=1
success=false

while [ "$attempt" -le "$max_attempts" ]; do
  status_code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9000/minio/health/live || true)
  if [ "$status_code" -eq 200 ]; then
    echo "MinIO is healthy (HTTP 200)."
    success=true
    break
  fi
  echo "Attempt $attempt/$max_attempts: MinIO returned HTTP $status_code. Retrying in 1s..."
  sleep 1
  attempt=$((attempt + 1))
done

if [ "$success" = false ]; then
  echo "Error: MinIO healthcheck failed to return 200 within 30 seconds."
  exit 1
fi

echo "Verifying Ollama contains llama3.2:3b model..."
# Wait up to 180 seconds because downloading a 2.0GB model could take some time depending on network speed.
max_attempts_ollama=90
attempt_ollama=1
model_found=false

while [ "$attempt_ollama" -le "$max_attempts_ollama" ]; do
  response=$(curl -s http://localhost:11434/api/tags || echo "{}")
  # Use jq to assert llama3.2:3b is present (checking exact and partial prefix matches)
  if echo "$response" | jq -e '.models[]? | select(.name == "llama3.2:3b" or .name == "llama3.2:3b-instruct" or (.name | startswith("llama3.2:3b")))' > /dev/null; then
    echo "Ollama model llama3.2:3b is present!"
    model_found=true
    break
  fi
  echo "Attempt $attempt_ollama/$max_attempts_ollama: Model llama3.2:3b not found yet. Retrying in 2s..."
  sleep 2
  attempt_ollama=$((attempt_ollama + 1))
done

if [ "$model_found" = false ]; then
  echo "Error: llama3.2:3b was not loaded in Ollama."
  echo "Current tags response:"
  curl -s http://localhost:11434/api/tags || true
  exit 1
fi

echo "All infrastructure checks passed successfully!"
