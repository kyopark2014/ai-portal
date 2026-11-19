---
name: doc-sharing
description: >
  생성된 artifact(xlsx/pptx/docx/pdf/png/csv/md/json 등)를 ai-portal S3의
  artifacts/{actor_id}/… 로 업로드하고 **presigned 다운로드 URL**을 반환한다.
  MCP가 아니라 code/shell 인터프리터에서 로컬 파일을 직접 S3로 PutObject 한다.
  트리거: doc-sharing, share_artifact, 산출물 공유, 다운로드 링크,
  artifact 업로드, 결과 파일 공유, markdown 다운로드.
---

# doc-sharing

Code Interpreter / shell 로 만든 산출물을 **프로젝트 S3 + presigned GET URL**로 공유한다.  
세션 로컬 경로(`/tmp/outputs/...`, `/mnt/workspace/...`)는 사용자가 열 수 없으므로, 최종 답변 전에 반드시 이 skill로 URL을 만든다.

## 환경

| 변수 | 의미 |
|------|------|
| `S3_BUCKET` | ai-portal 버킷 (예: `storage-for-ai-portal-…`) |
| `AWS_REGION` | 리전 (기본 us-west-2) |
| `actor_id` | Invoke 시 systemPrompt 에 명시된 값 (`actor_id: u-…`). 환경변수보다 이 값을 우선 |

`skills/doc-sharing/config.json`이 있으면 env가 비어 있을 때 fallback으로 사용한다.

## 워크플로우

```
- [ ] 1. 산출물을 로컬에 저장 (/tmp/outputs 또는 ARTIFACTS_DIR)
- [ ] 2. doc-sharing skill 동기화 (필요 시)
- [ ] 3. share_artifact.py 실행 → stdout JSON의 url(presigned) 확인
- [ ] 4. 최종 답변에 다운로드 URL 포함 (로컬 경로만 안내 금지)
```

### 1. skill 동기화

```bash
aws s3 sync s3://$S3_BUCKET/skills/doc-sharing/ /tmp/doc-sharing/
```

### 2. 공유 실행

```bash
python3 /tmp/doc-sharing/scripts/share_artifact.py \
  --filepath "/tmp/outputs/<file>.md" \
  --actor-id "<actor_id>"
```

성공 시 stdout 예:

```json
{
  "ok": true,
  "bucket": "storage-for-ai-portal-…",
  "key": "artifacts/<actor_id>/<file>.md",
  "url": "https://….amazonaws.com/artifacts/…?X-Amz-Algorithm=…",
  "download_url": "https://….amazonaws.com/artifacts/…?X-Amz-Algorithm=…",
  "expires_in": 43200,
  "actor_id": "<actor_id>"
}
```

## 규칙

- 최종 답변 전에 반드시 `url`(또는 `download_url`)을 확보한다. URL 없이 "생성 완료"만 하면 실패다.
- 로컬 경로(`/tmp/...`, `/mnt/workspace/...`)만 안내하는 것은 **금지**.
- `filepath`는 실제 저장된 절대 경로를 넘긴다.
- `actor_id`는 systemPrompt 의 `actor_id: …` 값을 그대로 사용한다 (닉네임·추측·빈 값 금지).
- 파일이 여러 개면 파일마다 스크립트를 각각 실행한다.

## 금지

- 로컬 경로만 주고 "화면에서 다운로드하세요"라고 안내하지 말 것
- MCP `share_artifact` / `artifact-share` 호출을 시도하지 말 것 (이 프로젝트는 skill만 사용)
