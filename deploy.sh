#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

set -euo pipefail

# Load environment variables from .env if present
if [[ -f ".env" ]]; then
  echo "Loading configuration from .env..."
  set -a
  source .env
  set +a
else
  echo "Error: .env file not found. Copy .env.example to .env and fill in your credentials first."
  exit 1
fi

# Validate required variables
REQUIRED_VARS=(
  "GOOGLE_CLOUD_PROJECT"
  "GOOGLE_CLOUD_PROJECT_NUMBER"
  "OAUTH_CLIENT_ID"
  "OAUTH_CLIENT_SECRET"
  "GOOGLE_PICKER_API_KEY"
)

for var in "${REQUIRED_VARS[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    echo "Error: Required environment variable $var is not set in .env"
    exit 1
  fi
done

REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-doc-reviewer-101}"
SA_NAME="doc-reviewer-sa"
SA_EMAIL="${SA_NAME}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"

echo "=================================================================="
echo " Deploying ${SERVICE_NAME} to Google Cloud Run"
echo " Project ID:     ${GOOGLE_CLOUD_PROJECT}"
echo " Project Number: ${GOOGLE_CLOUD_PROJECT_NUMBER}"
echo " Region:         ${REGION}"
echo "=================================================================="

echo "1. Enabling required Google Cloud APIs..."
gcloud services enable \
  aiplatform.googleapis.com \
  docs.googleapis.com \
  drive.googleapis.com \
  picker.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  --project="${GOOGLE_CLOUD_PROJECT}"

echo "2. Ensuring runtime Service Account (${SA_EMAIL}) exists..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Google Docs AI Reviewer Runtime SA" \
    --project="${GOOGLE_CLOUD_PROJECT}"
fi

echo "3. Granting Vertex AI & Observability IAM roles to ${SA_EMAIL}..."
for role in "roles/aiplatform.user" "roles/logging.logWriter" "roles/cloudtrace.agent"; do
  gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

echo "4. Deploying container to Cloud Run..."
# Note: OAUTH_REDIRECT_URI is intentionally omitted so the FastAPI app
# auto-detects its https://<service-url>.run.app/auth/callback URL.
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --allow-unauthenticated \
  --service-account="${SA_EMAIL}" \
  --min-instances=1 \
  --max-instances=1 \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION:-global},GOOGLE_CLOUD_PROJECT_NUMBER=${GOOGLE_CLOUD_PROJECT_NUMBER},OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID},OAUTH_CLIENT_SECRET=${OAUTH_CLIENT_SECRET},GOOGLE_PICKER_API_KEY=${GOOGLE_PICKER_API_KEY}"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --project "${GOOGLE_CLOUD_PROJECT}" --format="value(status.url)")

echo ""
echo "=================================================================="
echo " Deployment Complete!"
echo " Service URL: ${SERVICE_URL}"
echo "=================================================================="
echo ""
echo "NEXT STEPS (One-Time Google Cloud Console Setup):"
echo "1. Open https://console.cloud.google.com/apis/credentials?project=${GOOGLE_CLOUD_PROJECT}"
echo "2. Edit your OAuth 2.0 Client ID and add:"
echo "   - Authorized JavaScript origins: ${SERVICE_URL}"
echo "   - Authorized redirect URIs:      ${SERVICE_URL}/auth/callback"
echo "3. If your Browser API Key has Website restrictions, add:"
echo "   - ${SERVICE_URL}/*"
echo "=================================================================="
