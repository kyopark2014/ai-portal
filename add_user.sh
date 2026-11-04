#!/usr/bin/env bash
# Cognito 에 AI Portal MCP Gateway 사용자를 수동으로 만들고, 서버(gateway)별 사용 승인 그룹에 넣고, 토큰을 발급합니다.
# (awsome-ai-gateway/add_user.sh 와 같은 방식. 포털이 하는 일을 손으로 하는 것입니다.)
#
# 사용:
#   ./add_user.sh user@example.com                       # 사용자를 만들고 기본 Gateway(us-east-1)의 사용 승인 그룹에 넣는다
#   ./add_user.sh user@example.com --gateway us-east-1   # 어느 Gateway 의 사용 승인 그룹에 넣을지 지정
#   ./add_user.sh user@example.com --token               # + 토큰 발급·출력
#   ./add_user.sh user@example.com --no-group            # 사용자만 만든다(어느 Gateway 도 못 쓴다)
#   ./add_user.sh user@example.com --revoke us-east-1    # 사용 승인 취소(그룹 제거 + 리프레시 토큰 폐기)
#   ./add_user.sh --list-groups
#
# 그룹은 인터셉터 기준입니다: 인터셉터(lambda-interceptor)는 토큰의 cognito:groups 에 `mcp:<gateway-id>` 가 있어야
# 그 Gateway 호출을 허용합니다. 이 스크립트도 같은 이름 규칙(mcp:<gateway-id>)으로만 그룹을 다루며, 포털의 사용 승인과 같은 그룹입니다.
# 관리자가 직접 쓰는 스크립트라서 만든 즉시 사용 승인이 됩니다(포털의 신청·승인 절차를 거치지 않습니다).
#
# 설정: application/config.json (installer.py 가 기록). 환경변수가 있으면 우선합니다.
#   COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_REGION
#
# 신원 확인은 포털의 Google 로그인이 합니다. 이 스크립트는 그 사람을 Cognito 에 미리 만들어 둘 때 씁니다.
# 비밀번호는 아무도 모르는 무작위 값으로 정하고 저장·출력하지 않습니다(로그인은 서버/스크립트가 관리자 인증으로만 합니다).
# 토큰 출력에는 비밀(리프레시 토큰)이 들어 있으니 터미널 기록·화면 공유에 주의하세요.
set -euo pipefail

_ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${PORTAL_CONFIG:-$_ROOT/application/config.json}"

log() { printf "\033[1;36m[add_user]\033[0m %s\n" "$*" >&2; }
err() { printf "\033[1;31m[add_user:err]\033[0m %s\n" "$*" >&2; }
ok()  { printf "\033[1;32m[add_user]\033[0m %s\n" "$*" >&2; }
usage() { awk 'NR==1{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "$0"; exit "${1:-0}"; }

cfg() {  # config.json 에서 키 하나 읽기(없으면 빈 값)
  [ -f "$CONFIG" ] || return 0
  python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$CONFIG" "$1" 2>/dev/null || true
}

USER_POOL_ID="${COGNITO_USER_POOL_ID:-$(cfg cognito_user_pool_id)}"
CLIENT_ID="${COGNITO_CLIENT_ID:-$(cfg cognito_client_id)}"
REGION="${COGNITO_REGION:-$(cfg cognito_region)}"; REGION="${REGION:-us-east-1}"

DEFAULT_GATEWAY="${PORTAL_DEFAULT_GATEWAY:-us-east-1}"   # 기본으로 사용 승인할 Gateway 의 리전
EMAIL=""; GATEWAY=""; REVOKE=""; WANT_TOKEN=0; LIST_GROUPS=0; NO_GROUP=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --list-groups) LIST_GROUPS=1; shift ;;
    --token) WANT_TOKEN=1; shift ;;
    --no-group) NO_GROUP=1; shift ;;
    --gateway) [ $# -ge 2 ] || { err "--gateway 뒤에 리전이 필요합니다 (예: us-east-1)"; usage 1; }; GATEWAY="$2"; shift 2 ;;
    --revoke)  [ $# -ge 2 ] || { err "--revoke 뒤에 리전이 필요합니다 (예: us-east-1)"; usage 1; }; REVOKE="$2"; shift 2 ;;
    -*) err "알 수 없는 옵션: $1"; usage 1 ;;
    *) if [ -z "$EMAIL" ]; then EMAIL="$1"; shift; else err "인자가 너무 많습니다: $1"; usage 1; fi ;;
  esac
done

command -v aws >/dev/null 2>&1 || { err "aws CLI 가 필요합니다"; exit 1; }
[ -n "$USER_POOL_ID" ] && [ -n "$CLIENT_ID" ] || { err "Cognito 설정이 없습니다. 먼저: python installer.py --gateway-only  (또는 COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID 지정)"; exit 1; }
cog() { aws cognito-idp "$@" --region "$REGION"; }

if [ "$LIST_GROUPS" = "1" ]; then
  log "User Pool: $USER_POOL_ID ($REGION)"
  cog list-groups --user-pool-id "$USER_POOL_ID" --query 'Groups[].GroupName' --output table
  exit 0
fi

[ -n "$EMAIL" ] || { err "이메일이 필요합니다"; usage 1; }
case "$EMAIL" in *@*.*) ;; *) err "이메일 형식이 아닙니다: $EMAIL"; exit 1 ;; esac
EMAIL="$(printf '%s' "$EMAIL" | tr '[:upper:]' '[:lower:]')"

# 리전 → gateway id → 그룹 이름 (포털의 mcp_token.group_name 과 같은 규칙: mcp:<gateway-id>)
group_for() {
  local rgn="$1" key gid
  if [ "$rgn" = "us-west-2" ]; then key="gateway_id"; else key="gateway_$(printf '%s' "$rgn" | tr '-' '_')_id"; fi
  gid="$(cfg "$key")"
  [ -n "$gid" ] || { err "config.json 에 $rgn 의 gateway 가 없습니다 ($key). 먼저 python installer.py --gateway-only"; exit 1; }
  printf 'mcp:%s' "$gid"
}

# 승인 그룹을 따로 지정하지 않으면 기본 Gateway 의 사용 승인 그룹에 넣는다(취소·--no-group 일 때는 제외)
if [ -z "$GATEWAY" ] && [ -z "$REVOKE" ] && [ "$NO_GROUP" = "0" ]; then GATEWAY="$DEFAULT_GATEWAY"; fi

log "region=$REGION pool=$USER_POOL_ID email=$EMAIL gateway=${GATEWAY:-<없음>}"

# ── 사용자 생성(이미 있으면 그대로) ──
if cog admin-get-user --user-pool-id "$USER_POOL_ID" --username "$EMAIL" >/dev/null 2>&1; then
  log "이미 있는 사용자입니다 — 그대로 사용"
else
  log "Cognito 사용자 생성 중..."
  cog admin-create-user --user-pool-id "$USER_POOL_ID" --username "$EMAIL" \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
    --message-action SUPPRESS --query 'User.{Username:Username,Status:UserStatus}' --output table >&2
  ok "사용자 생성 완료"
fi

# ── 사용 승인 취소 ──
if [ -n "$REVOKE" ]; then
  G="$(group_for "$REVOKE")"
  log "사용 승인 취소: $G"
  cog admin-remove-user-from-group --user-pool-id "$USER_POOL_ID" --username "$EMAIL" --group-name "$G" || true
  cog admin-user-global-sign-out --user-pool-id "$USER_POOL_ID" --username "$EMAIL" >/dev/null   # 리프레시 토큰 폐기
  ok "취소 완료 (이미 발급된 액세스 토큰은 만료(최대 24시간)까지 남을 수 있습니다)"
fi

# ── 사용 승인(그룹) ──
if [ -n "$GATEWAY" ]; then
  G="$(group_for "$GATEWAY")"
  cog get-group --user-pool-id "$USER_POOL_ID" --group-name "$G" >/dev/null 2>&1 || {
    log "그룹 생성: $G"
    cog create-group --user-pool-id "$USER_POOL_ID" --group-name "$G" --description "사용이 승인된 사용자 (${G#mcp:})" >/dev/null
  }
  log "사용 승인: $G"
  cog admin-add-user-to-group --user-pool-id "$USER_POOL_ID" --username "$EMAIL" --group-name "$G"
fi

log "소속 그룹:"
cog admin-list-groups-for-user --user-pool-id "$USER_POOL_ID" --username "$EMAIL" --query 'Groups[].GroupName' --output table >&2

# ── 토큰 발급(서버가 하는 것과 같다: 무작위 비밀번호를 정하고 한 번만 로그인에 쓴다) ──
if [ "$WANT_TOKEN" = "1" ]; then
  [ -n "$GATEWAY" ] || { err "--token 은 사용 승인된 Gateway 가 있어야 합니다(--no-group 과 함께 쓸 수 없습니다)"; exit 1; }
  PW="$(python3 -c "import secrets;print(secrets.token_urlsafe(24)+'Aa1!')")"
  cog admin-set-user-password --user-pool-id "$USER_POOL_ID" --username "$EMAIL" --password "$PW" --permanent
  RESULT="$(cog admin-initiate-auth --user-pool-id "$USER_POOL_ID" --client-id "$CLIENT_ID" --auth-flow ADMIN_USER_PASSWORD_AUTH \
    --auth-parameters "USERNAME=$EMAIL,PASSWORD=$PW" --query 'AuthenticationResult.{access_token:AccessToken,refresh_token:RefreshToken,expires_in:ExpiresIn}' --output json)"
  PW=""
  ok "토큰 발급 완료 (액세스 토큰 24시간, 리프레시 토큰으로 갱신)"
  printf '%s\n' "$RESULT"
else
  ok "완료. 토큰이 필요하면 --token 을 추가하세요."
fi
