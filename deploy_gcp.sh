#!/bin/bash
# deploy_gcp.sh — build + deploy VITopia AI to Google Cloud Run (source deploy).
#
# Telegram (optional): export these before running to enable the bot —
#   export TELEGRAM_BOT_TOKEN=123456:AA...          # from @BotFather
#   export TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 16)
# The webhook URL is derived from the deployed service URL automatically.

set -e

PROJECT_ID="${GCP_PROJECT_ID:-nuveroai}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-vitopia-agent}"

echo "========================================================"
echo "🚀 Deploying ${SERVICE_NAME} to Cloud Run (${PROJECT_ID}/${REGION})"
echo "========================================================"
gcloud config set project "${PROJECT_ID}"

# --- env vars ----------------------------------------------------------------
ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${REGION},GEMINI_MODEL=gemini-2.5-flash,VIT_KB_DIR=./vit_knowledge_base"
[ -n "${VITOPIA_DEBUG}" ] && ENV_VARS="${ENV_VARS},VITOPIA_DEBUG=${VITOPIA_DEBUG}"
if [ -n "${TELEGRAM_BOT_TOKEN}" ] && [ -n "${TELEGRAM_WEBHOOK_SECRET}" ]; then
  ENV_VARS="${ENV_VARS},TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN},TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET}"
  TELEGRAM_ON=1
fi

echo "☁️  Building + deploying from source..."
# --update-env-vars (merge) so a redeploy without the Telegram vars exported
# does not wipe them off the running service.
gcloud run deploy "${SERVICE_NAME}" \
    --source . \
    --region "${REGION}" \
    --allow-unauthenticated \
    --no-cpu-throttling \
    --memory 1Gi --cpu 1 \
    --timeout 120 \
    --min-instances 0 --max-instances 10 \
    --update-env-vars "${ENV_VARS}"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format 'value(status.url)')
echo "✅ Deployed: ${SERVICE_URL}"

# --- Telegram webhook ------------------------------------------------------
if [ -n "${TELEGRAM_ON}" ]; then
  HOOK="${SERVICE_URL}/telegram/webhook"
  echo "🤖 Registering Telegram webhook: ${HOOK}"
  gcloud run services update "${SERVICE_NAME}" --region "${REGION}" \
      --update-env-vars "TELEGRAM_WEBHOOK_URL=${HOOK}"
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
       --data-urlencode "url=${HOOK}" \
       --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
       --data-urlencode 'allowed_updates=["message"]' \
       --data-urlencode 'drop_pending_updates=true'
  echo; echo "   Bot is live — open it in Telegram and send /start"
fi
