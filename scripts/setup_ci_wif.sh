#!/usr/bin/env bash
# One-time setup: let GitHub Actions (this repo only) act as a GCP service account without a key.
#
#   GitHub OIDC token --> Workload Identity Pool/Provider --> impersonate github-ci service account
#
# The service account gets only what the evals need: call Gemini, read BigQuery, search Vertex AI Search.
# Usage: bash scripts/setup_ci_wif.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-retail-agent-bk}"
REPO="${REPO:-b10000-coder/Multi-Agent-Retail-Assistant}"
SA_NAME="github-ci"
POOL="github"
PROVIDER="github-oidc"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project "$PROJECT_ID"

gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" --display-name="GitHub CI (evals)"

for role in roles/aiplatform.user roles/bigquery.jobUser roles/bigquery.dataViewer roles/discoveryengine.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${SA}" --role="$role" \
    --condition=None --quiet >/dev/null
  echo "granted $role"
done

gcloud iam workload-identity-pools describe "$POOL" --project "$PROJECT_ID" --location=global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" --project "$PROJECT_ID" --location=global --display-name="GitHub"

gcloud iam workload-identity-pools providers describe "$PROVIDER" --project "$PROJECT_ID" --location=global \
  --workload-identity-pool="$POOL" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project "$PROJECT_ID" --location=global \
    --workload-identity-pool="$POOL" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='${REPO}'"

# Only workflows from this repository may impersonate the service account.
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" \
  --quiet >/dev/null

echo
echo "Add these as GitHub repository VARIABLES (Settings > Secrets and variables > Actions > Variables):"
echo "  GCP_PROJECT            = ${PROJECT_ID}"
echo "  GCP_WIF_PROVIDER       = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo "  GCP_SERVICE_ACCOUNT    = ${SA}"
echo "  POLICY_SERVING_CONFIG  = (from scripts/setup_search.py output / retail_agent/.env)"
echo "  CATALOG_SERVING_CONFIG = (from scripts/setup_search.py output / retail_agent/.env)"
