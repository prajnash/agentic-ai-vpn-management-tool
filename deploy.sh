#!/usr/bin/env bash
# deploy.sh – Build and deploy the VPN Management Tool to Cloud Run
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="vpn-management-tool"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "▶ Project : ${PROJECT_ID}"
echo "▶ Region  : ${REGION}"
echo "▶ Image   : ${IMAGE}"
echo ""

# 1. Build and push
echo "── Building container image ──────────────────────────────────────────────"
gcloud builds submit --tag "${IMAGE}" .

# 2. Deploy
echo "── Deploying to Cloud Run ────────────────────────────────────────────────"
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10

# 3. Grab the service account and grant Firestore access
echo "── Granting Firestore permissions ───────────────────────────────────────"
SA=$(gcloud run services describe "${SERVICE_NAME}" \
       --region "${REGION}" \
       --format="value(spec.template.spec.serviceAccountName)")

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user"

# 4. Show the service URL
echo ""
echo "✅  Deployment complete."
gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --format="value(status.url)"
