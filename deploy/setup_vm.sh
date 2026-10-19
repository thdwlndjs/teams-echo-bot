#!/usr/bin/env bash
# Oracle Cloud "Always Free" VM(Ubuntu 가정)에서 딱 한 번 실행하는 초기 설정 스크립트.
#
# 전제:
#   - 이 저장소 코드가 이미 /opt/echo-bot 에 복사돼 있음 (scp/rsync로 옮겨둘 것)
#   - /opt/echo-bot/.env.production 에 실제 자격증명이 채워져 있음
#   - deploy/Caddyfile 의 도메인을 실제 값으로 바꿔둔 상태
#
# 사용법: sudo bash deploy/setup_vm.sh
set -euo pipefail

APP_DIR=/opt/echo-bot
SERVICE_USER=echobot

if [ "$(id -u)" -ne 0 ]; then
  echo "root(sudo)로 실행해줘: sudo bash deploy/setup_vm.sh" >&2
  exit 1
fi

echo "[1/6] 패키지 목록 갱신"
apt-get update -y

echo "[2/6] Python 3.12 확인/설치 (pyproject.toml이 요구하는 버전)"
if ! command -v python3.12 >/dev/null 2>&1; then
  # Ubuntu 24.04는 기본 python3가 이미 3.12라 이 분기를 안 탄다.
  # 22.04 등 구버전이면 deadsnakes PPA로 3.12를 받는다.
  apt-get install -y software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y python3.12 python3.12-venv
else
  apt-get install -y python3.12-venv
fi

echo "[3/6] Caddy 설치 (공식 apt 저장소)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "[4/6] 전용 시스템 사용자 생성 ($SERVICE_USER, 로그인 불가)"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

echo "[5/6] venv 생성 + 의존성 설치"
if [ ! -d "$APP_DIR/.venv" ]; then
  sudo -u "$SERVICE_USER" python3.12 -m venv "$APP_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo "[6/6] systemd 서비스 + Caddy 설정 반영, 방화벽 오픈"
cp "$APP_DIR/deploy/echo-bot.service" /etc/systemd/system/echo-bot.service
cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now echo-bot
systemctl reload caddy || systemctl restart caddy

# 로컬 방화벽(ufw). Oracle 콘솔의 Security List/NSG에서도 80/443을 별도로 열어줘야 한다 —
# 그건 VM 안이 아니라 OCI 콘솔에서 하는 작업이라 이 스크립트가 대신 못 함.
if command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp || true
  ufw allow 443/tcp || true
fi

echo "완료. 상태 확인: systemctl status echo-bot"
echo "잊지 말 것: OCI 콘솔 → VM의 Security List/NSG에서 80,443 포트 Ingress 규칙 추가!"
