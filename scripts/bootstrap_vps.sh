#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/qums-bot}"
SERVICE_NAME="${SERVICE_NAME:-qums-bot}"
REPO_URL="${REPO_URL:-https://github.com/tapendra9104/QUMS-Chat-Bot.git}"
BRANCH="${BRANCH:-main}"
PUBLIC_BASE_URL_DEFAULT="${PUBLIC_BASE_URL_DEFAULT:-http://45.196.196.19}"
LOCAL_TIMEZONE_DEFAULT="${LOCAL_TIMEZONE_DEFAULT:-Asia/Kolkata}"
OWNER_TELEGRAM_CONTACT_DEFAULT="${OWNER_TELEGRAM_CONTACT_DEFAULT:-@anonymous894}"
OWNER_WHATSAPP_CONTACT_DEFAULT="${OWNER_WHATSAPP_CONTACT_DEFAULT:-+919389411909}"
TELEGRAM_ADMIN_CHAT_IDS_DEFAULT="${TELEGRAM_ADMIN_CHAT_IDS_DEFAULT:-5570554765}"

prompt_required() {
  local label="$1"
  local current="${2:-}"
  local secret="${3:-0}"
  local value=""
  while [[ -z "$value" ]]; do
    if [[ "$secret" == "1" ]]; then
      read -r -s -p "$label: " value
      echo
    else
      if [[ -n "$current" ]]; then
        read -r -p "$label [$current]: " value
        value="${value:-$current}"
      else
        read -r -p "$label: " value
      fi
    fi
  done
  printf '%s' "$value"
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt update
apt install -y git python3 python3-venv python3-pip nginx ca-certificates

mkdir -p "$APP_DIR"
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

echo
echo "Enter deployment settings."
APP_SECRET_VALUE="$(prompt_required 'APP_SECRET' '' 1)"
ADMIN_USERNAME_VALUE="$(prompt_required 'ADMIN_USERNAME' 'admin' 0)"
ADMIN_PASSWORD_VALUE="$(prompt_required 'ADMIN_PASSWORD' '' 1)"
PUBLIC_BASE_URL_VALUE="$(prompt_required 'PUBLIC_BASE_URL' "$PUBLIC_BASE_URL_DEFAULT" 0)"
TWILIO_ACCOUNT_SID_VALUE="$(prompt_required 'TWILIO_ACCOUNT_SID' '' 0)"
TWILIO_AUTH_TOKEN_VALUE="$(prompt_required 'TWILIO_AUTH_TOKEN' '' 1)"
TELEGRAM_BOT_TOKEN_VALUE="$(prompt_required 'TELEGRAM_BOT_TOKEN' '' 1)"
TELEGRAM_ADMIN_CHAT_IDS_VALUE="$(prompt_required 'TELEGRAM_ADMIN_CHAT_IDS' "$TELEGRAM_ADMIN_CHAT_IDS_DEFAULT" 0)"
OWNER_TELEGRAM_CONTACT_VALUE="$(prompt_required 'OWNER_TELEGRAM_CONTACT' "$OWNER_TELEGRAM_CONTACT_DEFAULT" 0)"
OWNER_WHATSAPP_CONTACT_VALUE="$(prompt_required 'OWNER_WHATSAPP_CONTACT' "$OWNER_WHATSAPP_CONTACT_DEFAULT" 0)"
LOCAL_TIMEZONE_VALUE="$(prompt_required 'LOCAL_TIMEZONE' "$LOCAL_TIMEZONE_DEFAULT" 0)"

cat > "$APP_DIR/.env" <<EOF
ERP_BASE_URL=https://qums.quantumuniversity.edu.in
DATABASE_PATH=$APP_DIR/data/bot.sqlite3
APP_SECRET=$APP_SECRET_VALUE
APP_ENV=production
USE_WAITRESS=1
WAITRESS_THREADS=4
LOCAL_TIMEZONE=$LOCAL_TIMEZONE_VALUE
MORNING_DIGEST_TIME=06:30
EVENING_REPORT_TIME=19:00
ATTENDANCE_POLL_INTERVAL_MINUTES=1
SUBSTITUTION_POLL_INTERVAL_MINUTES=1
MONITOR_POLL_INTERVAL_MINUTES=1
SANDBOX_EXPIRY_WARNING_MINUTES=10
LECTURE_GRACE_MINUTES=20
DASHBOARD_AUTO_REFRESH_SECONDS=30
ATTENDANCE_CORRECTION_LOOKBACK_DAYS=14
ATTENDANCE_SHORTAGE_BUFFER_LECTURES=1
DELIVERY_RETRY_LIMIT=3
DELIVERY_RETRY_BACKOFF_SECONDS=60
LOW_ATTENDANCE_THRESHOLDS=75,70,65
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
ADMIN_USERNAME=$ADMIN_USERNAME_VALUE
ADMIN_PASSWORD=$ADMIN_PASSWORD_VALUE
PUBLIC_BASE_URL=$PUBLIC_BASE_URL_VALUE
TWILIO_ACCOUNT_SID=$TWILIO_ACCOUNT_SID_VALUE
TWILIO_AUTH_TOKEN=$TWILIO_AUTH_TOKEN_VALUE
TWILIO_WHATSAPP_MODE=sandbox
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_SANDBOX_JOIN_CODE=
TWILIO_STATUS_MESSAGE_LIMIT=50
TWILIO_CONTENT_SID_DEFAULT=
TWILIO_CONTENT_SID_MORNING=
TWILIO_CONTENT_SID_ATTENDANCE=
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN_VALUE
TELEGRAM_API_BASE_URL=https://api.telegram.org
TELEGRAM_ADMIN_CHAT_IDS=$TELEGRAM_ADMIN_CHAT_IDS_VALUE
TELEGRAM_POLL_INTERVAL_SECONDS=1
OWNER_TELEGRAM_CONTACT=$OWNER_TELEGRAM_CONTACT_VALUE
OWNER_WHATSAPP_CONTACT=$OWNER_WHATSAPP_CONTACT_VALUE
EOF

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=QUMS Bot
After=network.target

[Service]
User=root
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/nginx/sites-available/$SERVICE_NAME" <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
    }
}
EOF

rm -f /etc/nginx/sites-enabled/default
ln -sf "/etc/nginx/sites-available/$SERVICE_NAME" "/etc/nginx/sites-enabled/$SERVICE_NAME"

find "$APP_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
nginx -t
systemctl restart nginx

echo
echo "Deployment complete."
systemctl status "$SERVICE_NAME" --no-pager || true
echo
curl -I http://127.0.0.1:5000/ || true
echo
curl -I http://127.0.0.1:5000/admin/login || true
