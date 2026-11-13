#!/usr/bin/env bash
# Build the app image with Cloud Build and deploy app + MCP Toolbox sidecar to Cloud Run.
# Idempotent: re-run to deploy a new version.
# Usage: bash scripts/deploy_cloud_run.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-retail-agent-bk}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-retail-assistant}"
REPO="${REPO:-retail}"
TOOLBOX_VERSION="${TOOLBOX_VERSION:-1.16.0}"
TOOLBOX_SECRET="toolbox-config"
RUNTIME_SA_NAME="retail-assistant-run"

cd "$(dirname "$0")/.."
# Serving configs and model id come from the local agent config.
set -a; source retail_agent/.env; set +a
MODEL_ID="${MODEL_ID:-gemini-3.8-flash}"

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
TAG="$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)"
APP_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:${TAG}"
TOOLBOX_IMAGE="us-central1-docker.pkg.dev/database-toolbox/toolbox/toolbox:${TOOLBOX_VERSION}"

echo "==> APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT_ID"

echo "==> Artifact Registry repo"
gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION" --project "$PROJECT_ID"

echo "==> Runtime service account (least privilege)"
gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" --project "$PROJECT_ID" --display-name="Retail assistant (Cloud Run)"
for role in roles/aiplatform.user roles/bigquery.jobUser roles/bigquery.dataViewer roles/discoveryengine.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${RUNTIME_SA}" --role="$role" \
    --condition=None --quiet >/dev/null
done

echo "==> Toolbox config in Secret Manager"
gcloud secrets describe "$TOOLBOX_SECRET" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud secrets create "$TOOLBOX_SECRET" --replication-policy=automatic --project "$PROJECT_ID"
gcloud secrets versions add "$TOOLBOX_SECRET" --data-file=toolbox/tools.yaml --project "$PROJECT_ID" >/dev/null
gcloud secrets add-iam-policy-binding "$TOOLBOX_SECRET" --project "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor --quiet >/dev/null

echo "==> Build ${APP_IMAGE}"
gcloud builds submit --tag "$APP_IMAGE" --project "$PROJECT_ID" --region "$REGION" .

echo "==> Deploy ${SERVICE} to ${REGION}"
export PROJECT_ID REGION SERVICE RUNTIME_SA APP_IMAGE TOOLBOX_IMAGE TOOLBOX_SECRET MODEL_ID \
  POLICY_SERVING_CONFIG CATALOG_SERVING_CONFIG
RENDERED="$(mktemp)"
python3 -c 'import os,sys; sys.stdout.write(os.path.expandvars(open("deploy/service.yaml").read()))' > "$RENDERED"
gcloud run services replace "$RENDERED" --region "$REGION" --project "$PROJECT_ID"
rm -f "$RENDERED"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
echo
echo "Deployed: ${URL}  (private: requires IAM authentication)"
echo "Open it from your machine through an authenticated local proxy:"
echo "  gcloud run services proxy ${SERVICE} --region ${REGION} --project ${PROJECT_ID} --port 8081"
echo "  -> http://127.0.0.1:8081"
