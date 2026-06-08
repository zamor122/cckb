# Epic 1 - Local Infrastructure and MinIO Storage Layer

This repository contains the declarative infrastructure-as-code and test suite for the local storage and inference engine layers.

## Architecture

The system consists of the following Docker Compose services:
1. **MinIO Object Storage (`minio`)**: Official MinIO instance running on `localhost:9000` (API) and `localhost:9001` (Console).
2. **MinIO Initialization Sidecar (`createbuckets`)**: An ephemeral sidecar container that waits for MinIO to be healthy, connects using administrative credentials, ensures the bucket `cckb-data` is created (using `--ignore-existing` for idempotency), and sets the bucket policy to `public`.
3. **Ollama Inference Engine (`ollama`)**: Ollama engine running on `localhost:11434` with a persistent named Docker volume (`ollama_data`) for model caching.
4. **Ollama Model Loader Sidecar (`model-loader`)**: A lightweight sidecar container that waits for Ollama to be healthy and pulls the `llama3.2:3b` model. If network access is offline, the pull will log the error but exit gracefully without crashing the Ollama daemon.

## Setup and Running

1. **Environment Variables**:
   Create a `.env` file in the root directory (fallback credentials will automatically default to `minioadmin` / `minioadmin123` if not provided):
   ```env
   MINIO_ROOT_USER=minioadmin
   MINIO_ROOT_PASSWORD=minioadmin123
   ```

2. **Starting the Services**:
   Start the services in detached mode:
   ```bash
   docker compose up -d
   ```

3. **Tearing Down the Services**:
   Stop and clean up containers and volumes:
   ```bash
   docker compose down -v
   ```

## Testing

A Python virtual environment is set up locally to run tests:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest boto3
```

### 1. Unit Tests
Verifies that the `boto3` client constructor correctly ingests `endpoint_url='http://localhost:9000'` instead of defaulting to AWS production.
```bash
.venv/bin/pytest test_unit.py
```

### 2. Integration Tests
Runs the end-to-end infrastructure boot sequence, asserts MinIO healthcheck within 30 seconds, and queries the Ollama tags API using `jq` to verify `llama3.2:3b` is cached.
```bash
./test_infrastructure.sh
```

### 3. E2E Storage Tests
Generates a dummy text file, uploads it to the `cckb-data` bucket on MinIO, downloads it under a different name, asserts byte-level equality, and cleans up test files.
```bash
.venv/bin/python test_e2e_storage.py
```
