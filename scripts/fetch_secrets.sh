#!/usr/bin/env bash
set -euo pipefail

SECRET_NAME="${HAWKSOPTIONS_SECRET_NAME:-hawksoptions/keys}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
OUTPUT_FILE="${HAWKSOPTIONS_SHM_SECRET_FILE:-/dev/shm/.hawksoptions.env}"
MAX_AGE_SECONDS="${HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS:-0}"
FORCE_REFRESH="${HAWKSOPTIONS_SECRETS_FORCE_REFRESH:-0}"
REQUIRED_KEYS=(
  ALPACA_OPTIONS_PAPER_API_KEY
  ALPACA_OPTIONS_PAPER_SECRET_KEY
)
EXPORT_KEYS=(
  ALPACA_OPTIONS_PAPER_API_KEY
  ALPACA_OPTIONS_PAPER_SECRET_KEY
  ALPACA_OPTIONS_LIVE_API_KEY
  ALPACA_OPTIONS_LIVE_SECRET_KEY
  ALPACA_OPTIONS_DASHBOARD_PAPER_API_KEY
  ALPACA_OPTIONS_DASHBOARD_PAPER_SECRET_KEY
  ALPACA_OPTIONS_DASHBOARD_LIVE_API_KEY
  ALPACA_OPTIONS_DASHBOARD_LIVE_SECRET_KEY
  NEWS_API_KEY
  OPENAI_API_KEY
)

if [[ ! "$MAX_AGE_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS must be a non-negative integer" >&2
  exit 1
fi
MAX_AGE_SECONDS="$((10#$MAX_AGE_SECONDS))"

has_required_keys() {
  local path="$1"
  local key
  for key in "${REQUIRED_KEYS[@]}"; do
    grep -Eq "^${key}=" "$path" || return 1
  done
}

if [[ "$FORCE_REFRESH" != "1" && -s "$OUTPUT_FILE" ]]; then
  if ! chmod 600 "$OUTPUT_FILE" 2>/dev/null; then
    echo "existing $OUTPUT_FILE permissions could not be normalized; refreshing"
  elif ! has_required_keys "$OUTPUT_FILE"; then
    echo "existing $OUTPUT_FILE is missing required keys; refreshing"
  elif [[ "$MAX_AGE_SECONDS" == "0" ]]; then
    echo "reusing existing $OUTPUT_FILE"
    exit 0
  else
    now="$(date +%s)"
    mtime="$(stat -c %Y "$OUTPUT_FILE" 2>/dev/null || stat -f %m "$OUTPUT_FILE" 2>/dev/null || echo 0)"
    age=$((now - mtime))
    if (( age >= 0 && age < MAX_AGE_SECONDS )); then
      echo "reusing fresh $OUTPUT_FILE age=${age}s max_age=${MAX_AGE_SECONDS}s"
      exit 0
    fi
  fi
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 1
fi

SECRET_JSON="$(aws secretsmanager get-secret-value --secret-id "$SECRET_NAME" --region "$AWS_REGION" --query SecretString --output text)"
if [[ -z "$SECRET_JSON" || "$SECRET_JSON" == "None" ]]; then
  echo "no SecretString returned for $SECRET_NAME" >&2
  exit 1
fi

umask 077
OUTPUT_DIR="$(dirname "$OUTPUT_FILE")"
TEMP_FILE="$(mktemp "${OUTPUT_DIR}/.hawksoptions.env.XXXXXX")"
trap 'rm -f "$TEMP_FILE"' EXIT

{
  echo "# HawksOptions secrets"
  for key in "${EXPORT_KEYS[@]}"; do
    value="$(echo "$SECRET_JSON" | jq -r --arg k "$key" 'if (.[$k] // "") != "" then .[$k] | @sh else empty end')"
    if [[ -n "$value" ]]; then
      echo "$key=$value"
    fi
  done
} > "$TEMP_FILE"

if ! has_required_keys "$TEMP_FILE"; then
  echo "fetched secret is missing required paper trading keys" >&2
  exit 1
fi

chmod 600 "$TEMP_FILE"
mv "$TEMP_FILE" "$OUTPUT_FILE"
trap - EXIT
echo "wrote $OUTPUT_FILE"
