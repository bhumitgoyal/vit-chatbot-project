#!/bin/bash
# deploy_gcp.sh — build + deploy VITopia AI to Google Cloud Run (source deploy).
#
# Telegram (optional): export these before running to enable the bot —
#   export TELEGRAM_BOT_TOKEN=123456:AA...          # from @BotFather
#   export TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 16)
# The webhook URL is derived from the deployed service URL automatically.
#
# Assignment reminders: needs Firestore (native mode) in the project and a
# Cloud Scheduler job hitting /cron/assignment-reminders. Both are created
# here if missing; CRON_SECRET is generated and persisted if not exported.

set -e

PROJECT_ID="${GCP_PROJECT_ID:-nuveroai}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-vitopia-agent}"
SCHED_JOB="${SCHED_JOB:-vitopia-assignment-reminders}"
SCHED_CRON="${SCHED_CRON:-0 */3 * * *}"          # every 3 hours
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-nam5}"

echo "========================================================"
echo "🚀 Deploying ${SERVICE_NAME} to Cloud Run (${PROJECT_ID}/${REGION})"
echo "========================================================"
gcloud config set project "${PROJECT_ID}"

# --- reminder secret -------------------------------------------------------
# Reuse the value already on the running service if the caller didn't export one.
if [ -z "${CRON_SECRET}" ]; then
  CRON_SECRET=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" \
      --format='value(spec.template.spec.containers[0].env.filter("name:CRON_SECRET").extract("value").flatten())' 2>/dev/null || true)
fi
[ -z "${CRON_SECRET}" ] && CRON_SECRET=$(openssl rand -hex 16)

# --- env vars ----------------------------------------------------------------
ENV_VARS="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${REGION},GEMINI_MODEL=gemini-2.5-flash,VIT_KB_DIR=./vit_knowledge_base,CRON_SECRET=${CRON_SECRET}"
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
    --timeout 300 \
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

# --- Firestore + Cloud Scheduler for assignment reminders ----------------
# Non-fatal: a class deploy without the extra IAM still ships the app.
set +e
echo "🗓  Ensuring Firestore + Cloud Scheduler for assignment reminders..."
gcloud services enable firestore.googleapis.com cloudscheduler.googleapis.com --quiet

gcloud firestore databases describe --database='(default)' >/dev/null 2>&1
if [ $? -ne 0 ]; then
  echo "   Creating Firestore (native) in ${FIRESTORE_LOCATION}..."
  gcloud firestore databases create --location="${FIRESTORE_LOCATION}" --type=firestore-native --quiet
fi

CRON_URI="${SERVICE_URL}/cron/assignment-reminders"
if gcloud scheduler jobs describe "${SCHED_JOB}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHED_JOB}" --location "${REGION}" \
      --schedule "${SCHED_CRON}" --time-zone "Asia/Kolkata" \
      --uri "${CRON_URI}" --http-method POST \
      --update-headers "X-Cron-Secret=${CRON_SECRET}" \
      --attempt-deadline 600s
else
  gcloud scheduler jobs create http "${SCHED_JOB}" --location "${REGION}" \
      --schedule "${SCHED_CRON}" --time-zone "Asia/Kolkata" \
      --uri "${CRON_URI}" --http-method POST \
      --headers "X-Cron-Secret=${CRON_SECRET}" \
      --attempt-deadline 600s
fi
echo "   Scheduler job '${SCHED_JOB}' → ${CRON_URI} (${SCHED_CRON} Asia/Kolkata)"
set -e
