#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/qums-bot}"
SERVICE_NAME="${SERVICE_NAME:-qums-bot}"
APP_USER="${APP_USER:-qumsbot}"
REPO_URL="${REPO_URL:-https://github.com/tapendra9104/QUMS-Chat-Bot.git}"
BRANCH="${BRANCH:-main}"
SOURCE_MODE="${SOURCE_MODE:-git}"
ARCHIVE_PATH="${ARCHIVE_PATH:-}"
ENV_FILE_SOURCE="${ENV_FILE_SOURCE:-}"
SERVER_NAME="${SERVER_NAME:-_}"
PUBLIC_BASE_URL_DEFAULT="${PUBLIC_BASE_URL_DEFAULT:-}"
LOCAL_TIMEZONE_DEFAULT="${LOCAL_TIMEZONE_DEFAULT:-Asia/Kolkata}"

primary_server_name() {
  local first_name="${SERVER_NAME%% *}"
  printf '%s' "$first_name"
}

ssl_cert_available() {
  local cert_name
  cert_name="$(primary_server_name)"
  [[ -n "$cert_name" ]] &&
  [[ "$cert_name" != "_" ]] &&
  [[ -f "/etc/letsencrypt/live/$cert_name/fullchain.pem" ]] &&
  [[ -f "/etc/letsencrypt/live/$cert_name/privkey.pem" ]]
}

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

prompt_optional() {
  local label="$1"
  local current="${2:-}"
  local secret="${3:-0}"
  local value=""
  if [[ "$secret" == "1" ]]; then
    read -r -s -p "$label${current:+ [hidden]}: " value
    echo
  else
    if [[ -n "$current" ]]; then
      read -r -p "$label [$current]: " value
      value="${value:-$current}"
    else
      read -r -p "$label: " value
    fi
  fi
  printf '%s' "$value"
}

ensure_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this script as root."
    exit 1
  fi
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y git python3 python3-venv python3-pip nginx ca-certificates curl rsync tar
}

ensure_app_user() {
  if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  fi
}

sync_source_from_git() {
  mkdir -p "$APP_DIR"
  if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  else
    git -C "$APP_DIR" fetch origin
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
  fi
}

sync_source_from_archive() {
  if [[ -z "$ARCHIVE_PATH" ]]; then
    echo "ARCHIVE_PATH is required when SOURCE_MODE=archive."
    exit 1
  fi
  if [[ ! -f "$ARCHIVE_PATH" ]]; then
    echo "Archive not found: $ARCHIVE_PATH"
    exit 1
  fi

  mkdir -p "$APP_DIR"
  local extract_dir
  local source_dir
  extract_dir="$(mktemp -d)"
  tar -xzf "$ARCHIVE_PATH" -C "$extract_dir"

  if [[ -f "$extract_dir/README.md" ]]; then
    source_dir="$extract_dir"
  else
    source_dir="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    if [[ -z "$source_dir" ]]; then
      echo "Unable to determine extracted source directory."
      rm -rf "$extract_dir"
      exit 1
    fi
  fi

  rsync -a --delete \
    --exclude '.env' \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude 'logs/' \
    --exclude 'backups/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    "$source_dir"/ "$APP_DIR"/

  rm -rf "$extract_dir"
}

sync_source() {
  case "$SOURCE_MODE" in
    git)
      sync_source_from_git
      ;;
    archive)
      sync_source_from_archive
      ;;
    *)
      echo "Unsupported SOURCE_MODE: $SOURCE_MODE"
      exit 1
      ;;
  esac
}

build_venv() {
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
}

write_env_file() {
  local generated_secret
  local public_base_url_seed="$PUBLIC_BASE_URL_DEFAULT"
  if [[ -z "$public_base_url_seed" && "$SERVER_NAME" != "_" ]]; then
    public_base_url_seed="http://$SERVER_NAME"
  fi

  echo
  echo "Enter deployment settings."
  generated_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  local app_secret_value
  local admin_username_value
  local admin_password_value
  local public_base_url_value
  local telegram_bot_token_value
  local telegram_admin_chat_ids_value
  local admin_telegram_username_value
  local owner_telegram_contact_value
  local local_timezone_value

  app_secret_value="$(prompt_required 'APP_SECRET' "$generated_secret" 1)"
  admin_username_value="$(prompt_required 'ADMIN_USERNAME' 'admin' 0)"
  admin_password_value="$(prompt_required 'ADMIN_PASSWORD' '' 1)"
  public_base_url_value="$(prompt_optional 'PUBLIC_BASE_URL' "$public_base_url_seed" 0)"
  telegram_bot_token_value="$(prompt_optional 'TELEGRAM_BOT_TOKEN' '' 1)"
  telegram_admin_chat_ids_value="$(prompt_optional 'TELEGRAM_ADMIN_CHAT_IDS' '' 0)"
  admin_telegram_username_value="$(prompt_optional 'ADMIN_TELEGRAM_USERNAME' '' 0)"
  owner_telegram_contact_value="$(prompt_optional 'OWNER_TELEGRAM_CONTACT' '' 0)"
  local_timezone_value="$(prompt_optional 'LOCAL_TIMEZONE' "$LOCAL_TIMEZONE_DEFAULT" 0)"

  cat > "$APP_DIR/.env" <<EOF
ERP_BASE_URL=https://qums.quantumuniversity.edu.in
DATABASE_PATH=$APP_DIR/data/bot.sqlite3
APP_SECRET=$app_secret_value
APP_ENV=production
USE_WAITRESS=1
WAITRESS_THREADS=4
DASHBOARD_AUTO_REFRESH_SECONDS=30
RUN_SCHEDULER=1
TASK_QUEUE_MODE=inline
REDIS_URL=
TASK_QUEUE_NAME=qums-bot
ADMIN_USERNAME=$admin_username_value
ADMIN_PASSWORD=$admin_password_value
ADMIN_TELEGRAM_USERNAME=$admin_telegram_username_value
LOCAL_TIMEZONE=$local_timezone_value
MORNING_DIGEST_TIME=06:30
EVENING_REPORT_TIME=19:00
ATTENDANCE_POLL_INTERVAL_MINUTES=1
SUBSTITUTION_POLL_INTERVAL_MINUTES=1
MONITOR_POLL_INTERVAL_MINUTES=1
SANDBOX_EXPIRY_WARNING_MINUTES=10
LECTURE_GRACE_MINUTES=20
ATTENDANCE_CORRECTION_LOOKBACK_DAYS=14
ATTENDANCE_SHORTAGE_BUFFER_LECTURES=1
DELIVERY_RETRY_LIMIT=3
DELIVERY_RETRY_BACKOFF_SECONDS=60
LOW_ATTENDANCE_THRESHOLDS=75,70,65
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
PUBLIC_BASE_URL=$public_base_url_value
WEBHOOK_RATE_LIMIT_COUNT=60
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS=60
ADMIN_RATE_LIMIT_COUNT=10
ADMIN_RATE_LIMIT_WINDOW_SECONDS=60
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0.0
TELEGRAM_BOT_TOKEN=$telegram_bot_token_value
TELEGRAM_API_BASE_URL=https://api.telegram.org
TELEGRAM_ADMIN_CHAT_IDS=$telegram_admin_chat_ids_value
TELEGRAM_POLL_INTERVAL_SECONDS=1
LECTURE_SCHEDULE_POLL_INTERVAL_SECONDS=30
TELEGRAM_BOT_LINK=
OWNER_TELEGRAM_CONTACT=$owner_telegram_contact_value
EOF
}

ensure_env_file() {
  if [[ -n "$ENV_FILE_SOURCE" ]]; then
    if [[ ! -f "$ENV_FILE_SOURCE" ]]; then
      echo "ENV_FILE_SOURCE not found: $ENV_FILE_SOURCE"
      exit 1
    fi
    cp "$ENV_FILE_SOURCE" "$APP_DIR/.env"
  elif [[ -f "$APP_DIR/.env" ]]; then
    echo "Using existing $APP_DIR/.env"
  else
    write_env_file
  fi

  chmod 600 "$APP_DIR/.env"
}

write_systemd_unit() {
  cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=QUMS Bot
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

write_nginx_site() {
  local cert_name
  cert_name="$(primary_server_name)"

  if ssl_cert_available; then
    cat > "/etc/nginx/sites-available/$SERVICE_NAME" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAME;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $SERVER_NAME;

    ssl_certificate /etc/letsencrypt/live/$cert_name/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$cert_name/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_read_timeout 120s;
    }
}
EOF
    return
  fi

  cat > "/etc/nginx/sites-available/$SERVICE_NAME" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $SERVER_NAME;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_read_timeout 120s;
    }
}
EOF
}

fix_permissions() {
  mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
  find "$APP_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

start_services() {
  rm -f /etc/nginx/sites-enabled/default
  ln -sf "/etc/nginx/sites-available/$SERVICE_NAME" "/etc/nginx/sites-enabled/$SERVICE_NAME"

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  nginx -t
  systemctl restart nginx
}

print_summary() {
  echo
  echo "Deployment complete."
  systemctl status "$SERVICE_NAME" --no-pager || true
  echo
  curl -fsS http://127.0.0.1:5000/healthz || true
  echo
}

ensure_root
install_packages
ensure_app_user
sync_source
build_venv
ensure_env_file
write_systemd_unit
write_nginx_site
fix_permissions
start_services
print_summary
