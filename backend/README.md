# FastAPI Backend

## Run locally

```bash
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Environment variables:

- `DATA_GO_KR_SERVICE_KEY`: Korean government OpenAPI key
- `CORS_ALLOW_ORIGINS`: comma-separated allowlist (example: `https://your-frontend.web.app,http://localhost:3000`)

## Deploy to Cloud Run

Prerequisites:

- `gcloud` CLI installed and authenticated
- GCP project selected
- Cloud Run + Artifact Registry + Cloud Build APIs enabled

Set variables:

```bash
PROJECT_ID="your-gcp-project-id"
REGION="us-central1"
SERVICE_NAME="qi4u-backend"
DATA_GO_KR_SERVICE_KEY="replace-with-key"
```

Deploy:

```bash
gcloud config set project "$PROJECT_ID"

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "DATA_GO_KR_SERVICE_KEY=$DATA_GO_KR_SERVICE_KEY"
```

Optional CORS config during deploy:

```bash
--set-env-vars "CORS_ALLOW_ORIGINS=https://your-frontend-domain.com"
```
