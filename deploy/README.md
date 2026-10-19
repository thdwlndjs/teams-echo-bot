# Oracle Cloud "Always Free" 배포 런북

도커 없이 systemd + Caddy로 상시 실행. 여기 있는 파일들은 이미 준비돼 있고, 아래 순서대로
따라가면 된다. **1~3번은 본인이 직접 해야 하는 부분**(계정 생성/결제·신원 정보 입력, 도메인
DNS 설정은 대신 못 해줌).

## 1. Oracle Cloud 가입 (본인이 직접)

1. https://www.oracle.com/cloud/free/ 에서 가입.
2. **홈 리전을 신중하게 선택** — Ampere A1(무료 ARM VM) 재고가 리전마다 다르다. 가입 후엔
   홈 리전을 못 바꾸니, 가입 화면에서 "Always Free Ampere A1"이 되는 리전인지 먼저 확인.
3. 가입 완료 후 Compute → Instances → Create Instance:
   - Image: **Ubuntu 24.04** (기본 python3가 이미 3.12라 제일 간단함, 22.04여도 스크립트가 알아서 처리함)
   - Shape: **VM.Standard.A1.Flex** (Always Free), OCPU 1개/메모리 6GB 정도면 이 봇엔 충분히 넘침
   - SSH 키: 본인 공개키 등록(또는 새로 생성해서 다운로드해두기 — 나중에 접속용)
4. 인스턴스 생성 후 **Public IP** 확인해두기.
5. **Networking → Virtual Cloud Networks → (해당 VCN) → Security Lists**에서 **Ingress
   Rule로 TCP 80, 443 열기** (기본은 22만 열려 있음 — 이거 안 하면 Caddy가 떠도 외부에서 못 들어옴).

## 2. 도메인 준비 (본인이 직접)

기존 도메인이 있으면 서브도메인 하나(`bot.내도메인.com`)를 VM의 Public IP로 A 레코드 연결.

없으면 **DuckDNS**(무료, 카드 불필요):
1. https://www.duckdns.org 에서 로그인(Google/GitHub 등)
2. 원하는 서브도메인 등록(예: `내봇이름.duckdns.org`), IP에 VM의 Public IP 입력
3. 완료 — 이제 `내봇이름.duckdns.org`가 그 VM을 가리킴

## 3. 코드를 VM으로 복사 (본인 환경에서, 여기서 안내)

```bash
# 로컬(이 프로젝트 폴더)에서 실행 — <VM_IP>와 키 경로만 실제 값으로
rsync -avz --exclude '.venv' --exclude '__pycache__' \
  "C:/Users/aicam/Desktop/Seam/Teams_agent/echo-bot/" \
  ubuntu@<VM_IP>:/tmp/echo-bot-upload/

ssh ubuntu@<VM_IP> "sudo mkdir -p /opt/echo-bot && sudo cp -r /tmp/echo-bot-upload/* /opt/echo-bot/"
```

## 4. `.env.production` 확인

`/opt/echo-bot/.env.production`이 실제 값(CLIENT_ID/CLIENT_SECRET/TENANT_ID)으로 채워져 있고
`DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS`는 주석 처리(또는 없음)인지 확인.

## 5. `deploy/Caddyfile`의 도메인을 실제 값으로 수정

`your-domain.example.com` 자리를 2번에서 준비한 실제 도메인으로 바꾼다.

## 6. VM에서 설정 스크립트 실행

```bash
ssh ubuntu@<VM_IP>
sudo bash /opt/echo-bot/deploy/setup_vm.sh
```

Python 3.12 + venv 준비, Caddy 설치, 전용 시스템 사용자 생성, systemd 서비스 등록/기동,
로컬 방화벽(ufw) 오픈까지 이 스크립트 하나가 다 한다.

## 7. Azure Bot의 Messaging Endpoint 변경

Azure Portal → 해당 Bot 리소스 → Configuration → Messaging endpoint를:

```
https://<2번에서 준비한 도메인>/api/messages
```

로 변경 후 저장.

## 8. 확인

```bash
ssh ubuntu@<VM_IP>
systemctl status echo-bot     # active (running) 인지
journalctl -u echo-bot -f     # 실시간 로그
curl -I https://<도메인>       # Caddy가 정상 인증서 발급했는지 (200/404 등 응답이 오면 OK)
```

실제 Teams에서 봇에게 메시지 보내서 최종 확인.

## 나중에 코드 업데이트할 때

```bash
# 로컬에서 rsync 다시, 그다음 VM에서:
ssh ubuntu@<VM_IP> "sudo systemctl restart echo-bot"
```

시스템 패키지가 바뀐 게 아니면 `setup_vm.sh`를 다시 돌릴 필요는 없다 — 코드만 갱신하고
서비스 재시작하면 된다.
