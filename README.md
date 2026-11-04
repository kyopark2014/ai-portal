# AI Portal

AWS Agent Registry(Amazon Bedrock AgentCore)의 기능을 UI와 함께 보여주는 웹.
Prompt · MCP · Agent · Skill 을 카탈로그로 탐색·등록하고, 등록 정보는 Agent Registry 로 관리합니다.

- 구조: FastAPI REST(`/api/*`) + React(Vite) SPA, 한 컨테이너(:8501)로 ECS 배포 (agentic-work 방식)
- 로그인: Google. 관리자는 `ADMIN_EMAILS`(기본 `e2npark@gmail.com`), 그 외 로그인 사용자는 일반 사용자
- 저장소: SQLite(`application/data/portal.db`) — 목록·설명·Registry 연결 정보

```
브라우저 ─ Google ID token ─▶ POST /api/auth/google ─▶ HttpOnly 서명 쿠키
        ─ 쿠키 ─▶ /api/catalog/* , /api/chat (SSE)
                    │
                    ├─ Bedrock Prompt Management  (prompt 본문·버전)
                    ├─ AgentCore Registry `ai-portal` (등록 record)
                    ├─ AgentCore control (harness / runtime 동기화)
                    └─ AWSome AI Gateway (프롬프트 테스트: ChatAnthropic / ChatOpenAI)
```

## AgentCore Registry

**AWS Agent Registry**는 Amazon Bedrock AgentCore 안에 있는 **중앙 카탈로그**입니다. 조직 곳곳에 흩어진 MCP 서버·에이전트·스킬 같은 리소스를 한곳에 **등록하고, 검색하고, 승인(거버넌스)** 할 수 있습니다. 2026년 4월 프리뷰로 시작해 정식 출시되었고 `agent-registry` 네임스페이스를 씁니다(프리뷰의 `bedrock-agentcore` 네임스페이스는 2026-10-30 에 지원이 끝났습니다).

**해결하는 문제**
- **중앙화된 발견**: 리소스를 한곳에서 찾습니다.
- **승인 기반 큐레이션**: 보안·컴플라이언스·품질 기준을 통과한 레코드만 검색되게 합니다.
- **중복(sprawl) 방지**: 비슷한 에이전트가 따로따로 만들어지는 것을 줄입니다.

**등록할 수 있는 리소스**: MCP 서버 · 에이전트(A2A, Agent Card 스키마 0.3) · 스킬 · 커스텀 리소스. 이 포털은 MCP→`MCP`, Agent→`A2A`, Skill→`AGENT_SKILLS`, Prompt→`CUSTOM` descriptor 로 등록합니다(아래 [기능](#기능) 참조).

### 레코드 라이프사이클

```
CreateRegistryRecord ─▶ DRAFT ─ SubmitRegistryRecordForApproval ─▶ PENDING_APPROVAL
                                                                      │  UpdateRegistryRecordStatus (관리자)
                                                  ┌───────────────────┼───────────────┐
                                              APPROVED            REJECTED        (승인 후) DEPRECATED
                                           (검색·사용 가능)
```

1. Registry 를 만듭니다(인증 방식 선택, auto-approval 켜기/끄기).
2. `CreateRegistryRecord` 로 레코드를 등록합니다. 처음에는 잠깐 `CREATING` 이었다가 `DRAFT` 가 됩니다.
3. `SubmitRegistryRecordForApproval` 로 승인을 요청하면 `PENDING_APPROVAL` 이 됩니다.
4. 큐레이터가 `UpdateRegistryRecordStatus` 로 `APPROVED`(또는 `REJECTED`, 승인 후 `DEPRECATED`)로 바꿉니다.
5. 사람이나 에이전트가 **승인된 레코드만** 검색해 사용합니다.

조회 API: `SearchRegistryRecords`(하이브리드 검색), `ListDiscoverableRegistryRecords`(타입별 목록), `BatchGetDiscoverableRegistryRecord`(상세). URL 기반 **synchronization** 을 쓰면 라이브 MCP 서버의 도구 정의가 바뀔 때 메타데이터를 자동으로 맞춥니다.

### 핵심 기능
- **인증**: IAM 또는 JWT(OIDC/Cognito) 중에서 Registry 마다 선택합니다.
- **auto-approval**: Registry 를 만들 때 켜거나 끌 수 있는 승인 설정입니다(`approvalConfiguration.autoApproval`). 이 포털은 **끄고** 수동 승인(위 라이프사이클)을 씁니다.
- **하이브리드 검색**: 시맨틱 검색과 키워드 검색을 함께 수행합니다.
- **MCP 엔드포인트**: Registry 자체가 MCP 엔드포인트라서 에이전트가 직접 검색할 수 있습니다.

### 역할 분리

| 역할 | 하는 일 | 필요한 권한 |
|---|---|---|
| Administrator | Registry 생성, 인증 방식 결정, EventBridge 연동, IAM 관리 | — |
| Publisher | 레코드 작성, 승인 요청 | `CreateRegistryRecord`, `SubmitRegistryRecordForApproval` |
| Curator / Approver | 검토 후 승인·거절·deprecate | `UpdateRegistryRecordStatus` |
| Consumer | 승인된 레코드 검색·조회·MCP 연결 | 검색/목록 API |

Publisher 에게 `UpdateRegistryRecordStatus` 를 주지 않으면 **자기 승인**을 막을 수 있습니다. 이 포털은 같은 원칙을 앱 수준에서 적용합니다 — 승인·거절·deprecate 는 관리자만 할 수 있고(서버가 판단), 일반 사용자는 등록과 승인 요청만 합니다.

### 승인 알림
승인 요청이 왔다는 사실을 큐레이터에게 알리는 방법은 세 단계입니다.

| 단계 | 구성 | 이 포털 |
|---|---|---|
| 기본 | EventBridge → SNS 이메일 | 사용하지 않음 (구독자 전원에게 가서 승인자별로 나눌 수 없음) |
| 중간 | EventBridge → Lambda → (Slack/Teams 대신) **SES 이메일** | **사용 중** — 관리자 + 해당 record 의 reviewer 에게만 (아래 [AWS 리소스](#aws-리소스-installerpy)) |
| 고급 | Step Functions `waitForTaskToken` — 자동 사전 검사, 승인 버튼, 결과 반영(Inspector·GuardDuty 검사 연동 가능) | — |

### 알아둘 점
- **승인은 "검색에 노출되는가"만 정합니다.** Runtime·Gateway 의 실제 호출 권한은 IAM/OAuth 로 따로 관리해야 합니다.
- **가시성은 Registry 단위입니다.** Search/List 권한은 Registry 전체에 적용되므로, 일부 레코드를 특정 사용자에게만 숨기려면 Registry 를 나눠야 합니다. (이 포털은 한 Registry 를 쓰고, 포털 DB 에서 "내가 만든 것 + 승인된 것"만 보이게 따로 거릅니다.)
- 모든 API 호출은 **CloudTrail** 에 기록되고, `statusReason` 에 승인자 정보를 남길 수 있습니다(이 포털은 `[by 이메일]` 을 남깁니다).
- 여러 계정에서 공유하려면 **AWS RAM** 또는 **Organizations** 를 씁니다.

### 모범 사례 (Well-Architected)
- **AGENTREL04-BP02** — 에이전트를 역량 카테고리·스킬·제약·성능 프로필과 함께 등록하고, 오케스트레이터는 하드코딩된 식별자 대신 **시맨틱 검색**으로 에이전트를 찾습니다. 배포 파이프라인(CI/CD)에서 Registry 를 갱신해 **registry drift** 를 막습니다.
- **AGENTOPS01-BP02** — 핸드오프 때 Registry 로 피어의 존재·역량·작업 수용 가능 여부를 파악하고, Gateway 와 결합해 상호작용을 감사 가능하게 만듭니다. 에이전트→에이전트 에스컬레이션과 에이전트→사람 에스컬레이션은 **분리해서** 설계합니다.
- **툴 쿼터 거버넌스 패턴** — 쿼터 정책을 레코드의 커스텀 메타데이터에 두고 `schema_version` 으로 버전을 관리합니다. 쿼터 메타데이터가 없는 레코드는 자동 승인하지 않습니다. Registry 에 접근할 수 없을 때는 기본은 fail-open, 고비용·고위험 도구는 fail-closed 로 합니다.

### 참고 링크
- [Registry 개요](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html) · [시작하기](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-get-started.html) · [사용 사례](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-use-cases.html) · [주요 기능](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-key-capabilities.html)
- [EventBridge 알림](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-eventbridge.html) · [IAM 권한](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-iam-permissions.html) · [FAQ·마이그레이션](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html)
- [툴 접근 거버넌스(Prescriptive Guidance)](https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/govern-tool-access-with-aws-agent-registry-metadata.html) · [AGENTREL04-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentrel04-bp02.html) · [AGENTOPS01-BP02](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops01-bp02.html)

---

## 로컬 실행
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # GOOGLE_CLIENT_ID, LLM_GATEWAY_KEY 등 입력
./run_local.sh           # http://localhost:8501  (프런트 빌드 + uvicorn)
```
- Google Cloud Console → OAuth client → **Authorized JavaScript origins** 에 `http://localhost:8501` 과 배포 오리진을 추가해야 로그인됩니다.
- 프런트만 개발: `cd application/web && npm run dev` (:5173, `/api` 는 :8501 로 프록시)
- 테스트: `.venv/bin/python -m pytest` (테스트는 실제 AWS 를 호출하면 실패하도록 막혀 있음)
- AWS 자격 증명과 리전(`AWS_REGION`, 기본 `us-west-2`)이 필요합니다.

## 환경 변수 (`.env.example`)
| 변수 | 설명 |
|---|---|
| `GOOGLE_CLIENT_ID` | Google OAuth Web client ID (공개 값) |
| `ADMIN_EMAILS` | 쉼표 구분 **최초 관리자** 이메일. 화면에서 지울 수 없습니다. 추가 관리자는 화면에서 관리하며 SSM `/ai-portal/admins` 에 저장됩니다 |
| `SESSION_SIGNING_KEY` | 세션 쿠키 서명 키. 비우면 로컬에 자동 생성. ECS 는 Secrets Manager 로 주입 |
| `COOKIE_SECURE` | https 뒤에서는 `1` |
| `LLM_GATEWAY_URL` / `LLM_GATEWAY_KEY` | 프롬프트 테스트용 AWSome AI Gateway. 키는 서버에만 있고 브라우저로 나가지 않음 |
| `HARNESS_ARN` | (선택) MCP 테스트 harness. 기본은 `application/config.json` 의 `harness_arn` |
| `PORTAL_ORIGIN` | 승인 요청 메일의 링크 기준 주소(배포 오리진). 비우면 `config.json` 의 `sharing_url`, 없으면 `http://localhost:8501` |
| `GATEWAY_REGIONS` | MCP Gateway 를 가져올 리전(쉼표 구분). 비우면 `AWS_REGION` + us-east-1 |
| `REGISTRY_ID` | (선택) Registry 지정. 기본은 `application/config.json` 의 `registry_id` |
| `PORTAL_DB_PATH` | (선택) SQLite 경로 |

## 기능

**레지스트리 탐색** — Prompt / MCP / Agent / Skill 탭. 카드(`⋮` 메뉴: Open·Delete) / 목록 보기, 검색, "내가 만든 것" 필터.
등록하면 **한 번에** 저장과 Registry 등록이 끝납니다. Registry 등록만 실패하면 항목은 남고 상세 화면에서 재시도합니다.

| 종류 | 등록 | Registry descriptor | 비고 |
|---|---|---|---|
| Prompt | Bedrock Prompt Management 에 저장 → 불변 버전 게시 | `CUSTOM` (본문 없이 `promptArn`·`version`·`versionArn` 포인터만) | `{{변수}}` 자동 인식. 수정 시 새 버전 게시 후 record 포인터 갱신 |
| MCP | Endpoint URL + HTTP 헤더 직접 등록, 또는 **AgentCore Gateway 자동 가져오기** | `MCP` | 헤더 값은 목록/상세에서 마스킹. 직접 등록한 MCP 만 연결 테스트(`tools/list`). Gateway 는 IAM/JWT 인증이라 상세 정보만 표시. 사용 승인 흐름은 추후 |
| Agent | AgentCore harness·runtime 자동 동기화 + 직접 추가 | `A2A` | 설명 편집 가능 |
| Skill | Git(URL·subdirectory·credential ARN·user) 또는 S3(URI·version) | `AGENT_SKILLS` | 설명 기본값=Git/S3 정보, 등록 시 수정 가능. 이름은 소문자·숫자·하이픈으로 변환 |

- **동기화**: 로그인 시 백그라운드로 prompt(`list_prompts`/`get_prompt`), harness, runtime, **MCP Gateway(`list_gateways`/`get_gateway`, 현재 리전 + us-east-1)** 를 가져와 SQLite 에 upsert (5분 최소 간격, "새로고침"으로 수동). 사용자가 고친 설명은 덮어쓰지 않고, AWS 에서 사라진 동기화 항목은 제거.
- **프롬프트 테스트**: Prompt 를 Open → 상세 화면의 "테스트". 모델 선택 후 대화(SSE 스트리밍, 멀티턴). Prompt 가 system 메시지로 적용되며 변수 입력 가능. Claude → `ChatAnthropic`(`/v1/messages`, Bearer 헤더), 그 외 → `ChatOpenAI`(`/v1/chat/completions`, `max_completion_tokens`). Responses 전용 모델은 "지원 불가".
- **MCP 테스트**: MCP 를 Open → 상세 화면의 "테스트". harness agent `ai_portal` 이 선택한 MCP 를 그 호출의 도구로(또는 Skill 을 `skills` 로) 붙여 대화하고(SSE), 응답과 함께 **도구 호출·도구 결과**를 접이식으로 보여줍니다. IAM Gateway → `agentcore_gateway` 도구(harness 실행 역할이 SigV4 로 호출), 직접 등록한 MCP → `remote_mcp` 도구(URL + 헤더). `CUSTOM_JWT` Gateway 는 토큰이 필요해 지원하지 않음(422). 사용자 이메일은 해시한 `u-…` 값으로만 AWS(actorId)에 전달하고, 세션 ID 에 사용자를 섞어 다른 사용자의 세션을 재사용하지 못하게 합니다.
- **Skill 테스트**: Skill 을 Open → 상세 화면의 "테스트". MCP 테스트와 같은 방식으로 harness `ai_portal` 의 `skills` 에 선택한 skill 을 붙여 대화하고, `skills` 도구로 skill 을 불러오는 과정과 작업 결과를 보여줍니다. Git → `{git: {url, path, auth}}`, S3 → `{s3: {uri}}`. harness 는 `git clone` 을 하므로 GitHub 웹 주소(`…/tree/main/skills/docx`)는 **repo URL + 하위 경로로 분리**해서 보냅니다(subdirectory 를 따로 적으면 그것이 우선). `https://` 만 허용.
- **harness system prompt**: harness `ai_portal` 을 실행하는 모든 테스트(MCP·Skill·harness 종류 Agent)는 prompt 를 따로 지정하지 않으면 **`harness_prompt`**(Prompt Management 에 등록한 prompt)를 `systemPrompt` 로 씁니다. 테스트 화면의 "System prompt" 에서 다른 prompt 를 고르면 그것이 우선합니다(요청의 `prompt_id`). 기본 prompt 는 **승인(APPROVED)된 경우에만** 적용하고(미승인 내용이 모두에게 퍼지지 않게), 없거나 승인되지 않았으면 harness 자체 기본값을 씁니다. 본문은 포털 DB 사본이 아니라 Prompt Management 에서 읽어 수정이 바로 반영됩니다(현재 `DRAFT` 버전). 기본 prompt 를 읽지 못하면 조용히 harness 기본값으로, 사용자가 직접 고른 prompt 를 읽지 못하면 오류로 응답합니다. runtime 종류 Agent(`agentic_work` 등)에는 적용하지 않습니다. `{{변수}}` 가 있는 prompt 를 고르면 값이 채워지지 않은 채 전달됩니다.
- **Agent 테스트**: Agent 를 Open → 상세 화면의 "테스트"에서 프롬프트를 보내면 해당 agent 를 호출하고 응답과 도구 호출/결과를 보여줍니다. harness → `InvokeHarness`, runtime(HTTP) → `InvokeAgentRuntime`(agentic-work 와 같은 `prompt` payload, `model_name` 은 보내지 않아 agent 기본 모델 사용, 이전 대화는 프롬프트에 이어 붙임). MCP 프로토콜 runtime 은 MCP 서버라 MCP 탭에서 테스트. harness 가 만든 내부 runtime(`harness_*`)은 직접 호출 불가라 동기화에서 제외.
- **Registry 상태**: 상세 화면에서 상태 확인 / 승인 요청 / **승인 요청 취소**.
- **reviewer (record 별 승인 권한)**: **관리자는 항상 모든 record 를 승인**할 수 있고, 관리자가 record 마다 **reviewer 를 추가**하면 그 사람은 **그 record 만** 승인·거절할 수 있습니다(요청자는 승인자를 고르지 않습니다). 관리자는 Prompt/MCP/Skill/Agent 카드 메뉴(또는 목록, 상세 화면)의 **Add reviewer**에서 사용자를 **이메일·이름으로 검색**해 고릅니다(서버가 매 입력마다 검색해 일부만 내려주므로 사용자가 많아도 동작). reviewer 는 승인 메뉴에서 자기에게 지정된 record 만 보이고(그 외는 404), Deprecate·reviewer/관리자 관리·대시보드는 쓸 수 없습니다. reviewer 정보는 SSM `/ai-portal/reviewers` 에 저장되어 재배포 후에도 유지되며, SSM 을 읽지 못하면 reviewer 권한은 인정하지 않고 관리자만 승인합니다(fail closed). record 가 삭제·취소되면 그 reviewer 정보도 정리됩니다. 종류 단위(예: Skill 전체) reviewer 는 없고 record 별로만 지정합니다.
- **관리자 관리**: 승인 메뉴의 "승인자 관리" 탭(관리자만)에서 **관리자**를 추가·제거합니다(바로 반영, 재배포 불필요). 최초 관리자(`ADMIN_EMAILS`)와 자기 자신은 지울 수 없습니다. 목록은 컨테이너 DB 가 아니라 **SSM Parameter Store**(`/ai-portal/admins`)에 저장되며, SSM 을 읽지 못하면 최초 관리자만 관리자로 인정합니다(fail closed).
- **내 레코드**: "내 정보"에서 내가 만든 항목을 종류·Registry 상태별로 봅니다(승인 대기·거절·초안 포함, 상태 필터, Open 으로 상세). 관리자는 "내 것 / 전체" 를 전환해 모든 사용자의 항목(소유자 포함)을 볼 수 있고, 일반 사용자의 `scope=all` 요청은 403 입니다.
- **관리 대시보드(관리자)**: "관리" 메뉴. ① 접속 통계(접속한 사용자·최근 7일 활성·총 로그인) ② **Registry 등록 현황** — type(Prompt/MCP/Skill/Agent)별 승인·진행 상태 개수(AWS Registry 를 직접 조회) ③ **사용자 표** — 로그인 횟수·마지막 접속·소유한 항목을 type 별·상태별로 ④ 최근 접속 ⑤ 레지스트리 관리(Registry 선택, 새 Registry 생성). 접속 기록은 **이메일·이름·시각만** 저장하고(프로필 사진 URL 은 저장 안 함) 컨테이너 SQLite 라 재배포하면 초기화됩니다.
- **Registry 생성 기본값**: 인증 **IAM**, auto-approval **꺼짐**(수동 승인). 화면에서 JWT(OIDC discovery URL 필수) 나 auto-approval 켜기도 고를 수 있고, 켜면 경고가 나옵니다.
- **승인(관리자)**: 상단 "승인" 메뉴 또는 메일 링크(`/approvals/<recordId>`). Registry 를 직접 조회해 대기·승인됨·거절됨·Deprecated 탭으로 보고, record 내용을 검토한 뒤 승인/거절(사유 필수)/Deprecate 합니다. AWS 의 현재 상태를 확인하고 처리해 이중 처리를 막으며(409), 사유에 처리자 이메일(`[by …]`)을 남깁니다. 일반 사용자는 403.
- **삭제**: Registry record → Prompt Management → 포털 DB 순. record 가 `CREATING` 이면 409(등록 직후 몇 초간 삭제 불가). AWS 동기화 항목은 포털 목록에서만 지워짐.
- **Registry 기반 복원**: 동기화 마지막 단계(`sync_registry`)가 Registry 의 record 를 읽어 포털 항목과 연결합니다. DB 가 비어 있어도(재배포·신규 서버) 복구되며, 승인 상태도 Registry 에서 가져옵니다. prompt(Prompt Management 포인터)·skill(Git/S3 출처)·agent(ARN)·MCP(URL)를 record 내용에서 복원합니다. 같은 대상의 동기화 항목(prompt·agent·Gateway)이 있으면 그 항목에 연결하고(중복 생성 안 함), 없으면 새로 만듭니다. 이미 연결된 항목은 상태만 갱신하며 사용자가 고친 설명은 덮어쓰지 않습니다. `CREATING`/`UPDATING` 중인 record 와 포털이 만들지 않은 형식의 record 는 건너뜁니다. **복원되지 않는 것**: MCP 의 HTTP 헤더(Registry 에 저장하지 않음), 원래 소유자(→ 소유자 `registry`, 관리자만 수정·삭제).
- **가시성**: 관리자는 모든 항목을 봅니다. **일반 사용자는 자기가 만든 항목과 Registry 에서 승인(APPROVED)된 항목만** 봅니다(거절·deprecated 된 남의 항목은 숨김, 내가 만든 항목은 상태와 무관하게 보임). 목록·검색뿐 아니라 ID 로 접근하는 모든 API(수정·삭제·등록·테스트 등)에 같은 규칙을 적용하며, 숨겨진 항목은 403 이 아니라 404 로 응답해 존재 여부를 알리지 않습니다. 승인된 남의 항목은 보고 테스트에 쓸 수 있지만 수정·삭제는 불가.
- **권한**: 수정·삭제·등록은 소유자 또는 관리자. Registry 생성·선택은 관리자만.

## AWS 리소스 (`installer.py`)
```bash
python installer.py --registry-only        # Registry `ai-portal` 만
python installer.py --notifications-only   # 승인 요청 메일 알림만
python installer.py --harness-only         # MCP 테스트용 harness agent `ai_portal` 만
python installer.py --domain-only          # 커스텀 도메인(인증서·CloudFront 별칭·DNS)만
python installer.py                        # 전체 (VPC·ALB·ECR·ECS Fargate·CloudFront)
python installer.py --skip-docker-build    # 이미지 재빌드 없이
```
- **Harness agent `ai_portal`**: MCP 테스트용(`harness-work` 방식). `harnessName` 은 하이픈 불가라 `ai_portal`. 모델 `us.anthropic.claude-sonnet-5-5`, 전용 역할 `role-harness-for-ai-portal-us-west-2`(Bedrock 모델 호출 + `InvokeGateway` + harness 메모리). ID/ARN 은 `application/config.json`(`harness_id`, `harness_arn`) (값은 `config.json` 의 `harness_id` 참조). 도구는 호출마다 지정하므로 harness 자체는 MCP 와 무관합니다.
- **Registry `ai-portal`**: IAM 인증, auto-approval 꺼짐. ID 는 `application/config.json`(`registry_id`)에 기록 (us-west-2, 값은 `config.json` 의 `registry_id` 참조).
- **승인 요청 메일**: EventBridge 규칙 `ai-portal-approval-requested`(`Registry Record State changed to Pending Approval`, 이 Registry 만) → Lambda `ai-portal-approval-notify`(`lambda-notify/`) → **SES**. 수신자는 **모든 관리자 + 그 record 에 지정된 reviewer** 입니다(reviewer 는 자기 record 알림만 받고, 관리자와 겹치면 한 통만). 메일에 승인 링크(`{PORTAL_ORIGIN}/approvals/<recordId>`)가 들어갑니다. 발신 주소는 `ai-portal@my-agentic-ai.click` 하나로 IAM 조건 키로 고정되고, 도메인은 DKIM 인증을 마쳤습니다. **SES 샌드박스 제약**: 이 계정의 SES 는 프로덕션 액세스가 꺼져 있어, *인증된 수신 주소로만* 발송됩니다(하루 200통). 승인자를 추가할 때마다 그 주소를 SES 에 등록·인증하거나 SES 프로덕션 액세스를 신청해야 합니다. 링크 주소를 바꾸려면 `PORTAL_ORIGIN` 설정 후 `--notifications-only` 를 다시 실행합니다.
- **전체 설치(실행됨, 2026-11-04)**: 새 VPC `10.23.0.0/16`, ALB(보안 그룹이 CloudFront origin-facing prefix list 의 80 만 허용 → 인터넷에서 ALB 직접 접속 불가, 안쪽은 `X-Origin-Verify` 비밀 헤더), ECR(arm64), ECS Fargate desired=1, CloudFront(HTTPS 강제·보안 헤더), Secrets Manager(`ai-portal/session-signing-key`, `llm-gateway-key`). **배포 URL: https://portal.my-agentic-ai.click** (`config.json` 의 `sharing_url`; CloudFront 기본 주소(`config.json` 의 `cloudfront_domain`)도 계속 동작). 사용하는 주소를 Google OAuth client 의 **Authorized JavaScript origins** 에 추가해야 로그인됩니다. 이미지에는 `config.json` 이 들어가지 않으므로 Registry ID·harness ARN 은 ECS 환경 변수(`REGISTRY_ID`, `HARNESS_ARN`)로 전달합니다. 보안 그룹 이름은 `sg-` 로 시작할 수 없습니다(AWS 제약). 재배포: 코드 변경 후 `python installer.py`(이미지를 다시 빌드·푸시하고 ECS 를 강제 재배포), 이미지 재빌드 없이 인프라만은 `--skip-docker-build`.
- ECS task role: Prompt Management, AgentCore 조회·Registry record 권한 (Bedrock 모델 직접 호출 권한은 없음 — 테스트는 Gateway 경유).

## API 요약
| | |
|---|---|
| `GET /api/config` · `POST /api/auth/google` · `GET /api/me` · `POST /api/auth/logout` | 인증 |
| `GET /api/catalog/{kind}?q=` · `PATCH …/{id}/description` · `DELETE …/{id}` | 목록·설명·삭제 (kind = prompt\|mcp\|agent\|skill) |
| `POST /api/prompts` · `PUT /api/prompts/{id}` · `POST …/{id}/refresh` | Prompt (Bedrock 저장·수정·재조회) |
| `POST /api/mcp` · `POST /api/mcp/{id}/test` · `POST /api/skills` · `POST /api/agents` | MCP·Skill·Agent 등록 |
| `POST /api/catalog/{kind}/{id}/register` · `…/registry/refresh` · `…/registry/submit` · `…/registry/cancel` | Registry 등록·상태·승인 요청·취소 |
| `GET/POST /api/registries` · `PUT /api/registries/selected` | Registry 목록·생성·선택 (생성·선택은 관리자) |
| `GET/POST /api/sync` | 동기화 상태·수동 실행 |
| `GET /api/gateway/models` · `POST /api/chat` | 프롬프트 테스트 (SSE) |
| (공통) | MCP·Skill·Agent 테스트 요청은 선택 필드 `prompt_id`(harness system prompt)를 받음 |
| `POST /api/skills/{id}/chat` | Skill 테스트 — harness 에 skill 을 붙여 호출 (SSE) |
| `POST /api/agents/{id}/chat` | Agent 테스트 — harness/runtime 호출 (SSE) |
| `GET /api/mcp/test-status` · `POST /api/mcp/{id}/chat` | MCP 테스트 — harness agent (SSE: 텍스트·도구 호출·도구 결과) |
| `GET /api/approvals?status=` · `GET /api/approvals/{recordId}` · `POST /api/approvals/{recordId}/decision` | 승인 큐·검토·승인/거절 (관리자 + 해당 record 의 reviewer; deprecate 는 관리자만) |
| `POST …/registry/submit` | 승인 요청 |
| `GET /api/reviewers/users?q=` · `GET/POST/DELETE /api/reviewers` | reviewer 후보 사용자 검색 · record 별 reviewer 조회·추가·제거 (관리자 전용) |
| `GET/POST/DELETE /api/admins` | 관리자(승인 가능한 사람) 조회·추가·제거 (관리자 전용) |
| `GET /api/my/records?scope=mine\|all` | 내 레코드 (`all` 은 관리자만) |
| `GET /api/admin/dashboard` | 접속 통계 · Registry type/상태별 현황 · 사용자별 등록 (관리자 전용) |
| `GET /api/registries/defaults` · `POST /api/registries` | Registry 생성 기본값(IAM, auto-approval 꺼짐) · 생성(관리자) |
| `GET /api/admin/ping` | 관리자 전용 골격 |

## 알아둘 제약
- **SES 샌드박스**: 승인 알림은 인증된 수신 주소로만 발송됩니다. 새 승인자를 추가하면 그 주소를 SES 에 등록·인증해야 메일을 받습니다(프로덕션 액세스를 신청하면 해소).
- **harness 의 description**: harness 에는 설명 필드가 없어 동기화 시 알려진 harness 의 기본 설명을 씁니다(`ai_portal` → "AI Portal 운영을 위한 Agent"). 사용자가 고친 설명은 유지됩니다. 또 **승인된 Registry record 의 설명이나 내용을 수정하면 record 가 `DRAFT` 로 되돌아가 다시 승인을 받아야 합니다.**
- **배포본과 로컬의 DB 는 별개**: Registry 에 record 가 있어도 포털 항목과의 연결은 각 서버의 SQLite 에 있습니다. 서버는 로그인 시 동기화(Registry 복원 포함)로 채워집니다. 복원된 항목의 소유자는 `registry` 입니다.
- **배포 중 일시 중단**: 단일 태스크(desired=1, `minimumHealthyPercent=0`)라 재배포 동안(수 분) 503 이 됩니다. 무중단은 태스크 2개 이상 + 영구 저장소가 필요합니다.
- **동기화 항목의 공개 범위**: AWS 에서 가져온 항목(harness·runtime·Gateway·AWS 의 prompt)은 Registry 에 등록·승인되기 전에는 승인 상태가 없어 일반 사용자에게 보이지 않습니다. 관리자가 Registry 에 등록해 승인해야 공개됩니다. 포털의 승인 상태는 "상태 확인"·승인 처리 때만 갱신되므로 AWS 콘솔에서 직접 처리하면 늦게 반영됩니다.
- **Gateway 동기화 범위**: 기본은 `AWS_REGION`(us-west-2) + us-east-1 이며 `GATEWAY_REGIONS`(쉼표 구분)로 바꿉니다. 리전별로 독립 처리해 한 리전 조회가 실패해도 그 리전의 기존 항목은 유지됩니다(같은 Gateway ID 가 리전마다 있어도 구분됨). Gateway 의 target(도구) 목록은 아직 보여주지 않습니다.
- **승인 요청 취소**: AWS 가 `PENDING_APPROVAL → DRAFT` 되돌림을 지원하지 않고, `REJECTED` 로 바꾸면 재제출이 막힙니다. 그래서 취소는 record 를 **삭제**하고 연결을 비웁니다(다시 등록해야 함). 이미 간 알림 메일은 회수되지 않습니다. Prompt 본문·버전은 Prompt Management 에 남습니다.
- **승인의 의미**: 승인은 Registry 검색에 노출될지만 정합니다. 승인된 MCP·agent 의 실제 사용 권한(IAM/OAuth)은 별도입니다. 메일 링크는 로그인한 관리자만 열 수 있어 전달돼도 일반 사용자는 승인할 수 없습니다.
- **Registry 삭제와 Prompt**: 삭제는 record 를 먼저 지워 "가리키는 대상 없는 record" 가 남지 않게 합니다.
- **SQLite 영속성**: ECS 컨테이너 로컬이라 재배포 시 사라집니다(prompt·agent 는 재동기화되지만 MCP·Skill 등록 정보와 편집한 설명은 유실). 영구 저장소(EFS 등)는 미정.
- **Gateway 키 공유**: 현재 agentic-work 의 키를 복사해 사용 중이라 사용량이 그쪽에 합산됩니다. 별도 키 발급 권장. 로그인한 모든 사용자가 같은 키로 호출합니다(사용자별 제한 없음).
- **Gateway 모델**: 목록에 보여도 키·구독 상태에 따라 호출이 실패할 수 있습니다(예: Marketplace 구독 필요, 일부 모델 400/502).
- `application/config.json`(계정 ID·Registry ARN·CloudFront ID 등 계정 고유 값)은 `installer.py` 가 만드는 파일이며 **저장소에 올리지 않습니다**(`.gitignore`). 새로 clone 했다면 `python installer.py`(또는 `--registry-only`)로 다시 만들거나 `REGISTRY_ID`·`HARNESS_ARN` 환경 변수를 지정하세요.

- **Skill 테스트의 실행 환경**: harness 기본 이미지(Amazon Linux 2023)에는 `node`/`npm` 이 없어서, Skill 테스트는 harness 호출 전에 세션마다 한 번 `InvokeAgentRuntimeCommand` 로 설치합니다(`mcp_test.ensure_node_installed`, harness-work 의 `220e457` 방식). 이 이미지는 `apt-get` 이 아니라 microdnf 라서 `dnf install -y nodejs npm`(Node 18, `-q` 불가)을 먼저 쓰고 `apt-get` 은 폴백입니다. 명령은 runtime ARN 이 아니라 **harness ARN** 으로 호출해야 합니다(runtime ARN 은 `managed by a harness` 로 거부). 설치는 best effort 라 실패해도 대화는 계속되고, 첫 호출은 설치로 20초가량 더 걸립니다. MCP 테스트에는 설치하지 않습니다. `docx` 같은 npm 패키지는 세션 안에서 agent 가 직접 설치합니다(세션이 바뀌면 다시). `soffice`·`pandoc` 은 없어 PDF 변환 검증은 불가. repo 전체처럼 SKILL.md 가 여러 개인 주소는 거부되므로 skill 하나를 가리키는 경로가 필요합니다.
- **Agent 테스트의 입력 규약**: runtime 마다 payload 규약이 달라, agentic-work 방식(`prompt` 중심)을 따르는 runtime 만 확실히 동작합니다(`agentic_work`, `toons_viewer` 확인). 다른 규약은 응답이 비거나 오류가 날 수 있습니다. 호출은 서버의 IAM 권한으로 이뤄지므로 ECS task role 에 `InvokeHarness`/`InvokeAgentRuntime` 이 필요합니다(`installer.py` 에 포함).
- **MCP 테스트의 권한 범위**: harness 실행 역할이 이 계정의 모든 Gateway(`gateway/*`)를 호출할 수 있어, 로그인한 누구나 포털에서 IAM Gateway 의 도구를 실행해 볼 수 있습니다. 직접 등록한 MCP 의 헤더(인증 정보)는 harness 호출에 그대로 전달됩니다. 필요하면 허용 Gateway 를 지정해 범위를 좁히세요. harness 는 Bedrock 모델을 직접 쓰므로 AWSome AI Gateway 를 거치지 않습니다.
- **도구가 없는 Gateway**: `harness-work`·`harness-skills`·`agentcore-harness` 는 target 이 0개라 테스트해도 호출할 도구가 없습니다.

## 로드맵 (미구현)
- MCP 서버 **사용 승인** 흐름
- Gateway target(도구) 목록
- Registry **hybrid 검색**(`SearchRegistryRecords`) 과 Registry 기준 목록
- 이벤트·감사 로그 패널
- SQLite 영구 저장소(현재 컨테이너 로컬이라 재배포 시 유실 — 등록 항목은 Registry 에서 복원되지만 MCP 헤더·소유자·편집한 설명은 복원 안 됨)
