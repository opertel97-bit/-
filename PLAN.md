# 릴스 자동 예약 업로드 시스템 구축 계획 (로컬 세션 인계용)

채널: 유튜브 '운동과 건강함'
목표: n8n + Claude Code + 어사이드 브라우저로 인스타그램/페이스북 릴스 자동 예약 업로드

## 0. 새 세션이 먼저 알아야 할 것
- 사용자는 n8n을 거의 모른다. 설치, 설정, 코드 작성, 테스트는 Claude가 직접 수행한다.
- 사용자가 직접 해야 하는 것은 **로그인, 인증, 권한 승인**뿐이다. 그 시점에만 단계별로 안내한다.
- **실제 공개 게시 전에는 반드시 사용자 승인**을 받는다. 영상 1개로 정상 작동을 먼저 검증한다.
- 환경: Windows PC (`DESKTOP-OA1UUPG`), 영상 폴더 후보: `footage`, `New cat`, `클로드실험`, `고양이 정리`. **MP4/SRT가 실제로 어느 폴더에 있는지 사용자에게 확인할 것.**
- 이전 클라우드 세션은 내 PC에 접근하지 못해서 분석까지만 했다.

## 1. 요구사항
- Windows에서 n8n 무료 자체 설치 버전 사용
- n8n 공식 MCP 연결 (Claude Code에서 n8n 워크플로우 조작)
- 어사이드 브라우저로 웹 설정 (Meta 개발자 앱, Google, n8n UI)
- PC에 저장된 MP4, SRT 활용
- 영상별 IG/FB 캡션 자동 생성 (공유, 팔로우, 유튜브 본편 유입 유도)
- 구글 스프레드시트로 예약 관리
- 하루 3개 자동 게시, 수백 개 대량 예약 지원
- 공식 Meta Graph API 사용
- 실패 알림, 중복 게시 방지
- 한국 시간(Asia/Seoul) 기준 예약

## 2. 기존 템플릿 분석
템플릿: n8n #19738 "Schedule and publish Instagram and Facebook Reels from Google Sheets with Gemini" (작성자 Ujjawal Solanki)
- 페이지: https://n8n.io/workflows/19738-schedule-and-publish-instagram-and-facebook-reels-from-google-sheets-with-gemini/
- 워크플로우 JSON 다운로드: `https://api.n8n.io/api/templates/workflows/19738` (응답의 `workflow.workflow`가 import용 JSON)

### 노드 흐름
1. `Schedule • Every 30 Minutes` 스케줄 트리거
2. `Google Sheets • Get Next Pending` : `Queue` 탭 읽기
3. `Filter • Due + Pending` : 예약 시간 도래 + PENDING 필터
4. `Limit` : 1건만 처리
5. `Code • Prepare Processing` / `Google Sheets • Mark Processing` : 처리 중 표시, retry_count 관리
6. `AI • Generate IG + FB Captions` (Gemini, Structured Output Parser) : 캡션 JSON 생성
7. Instagram: `Create Reel`(media) → `Wait` 30초 → `Check Reel Status` → `Check Status`(switch) → `Publish Reel`(media_publish)
8. Facebook: `Start Reel Upload`(video_reels, upload_phase=start) → `Upload Reel`(HTTP, upload_url로 업로드) → `Publish Reel`(upload_phase=finish)
9. `Instagram • Result`, `Facebook • Result`, `Merge`, `Calculate Final Status` : IG/FB 개별 상태 합산
10. `Google Sheets • Final Status` : status, retry_count, instagram_status, facebook_status 기록

### 템플릿의 시트 컬럼
`id`, `cloudinary_url`(공개 영상 URL), `publish_date`(DD-MM-YYYY), `publish_time`(HH:mm), `status`(PENDING/POSTED), 그 외 `retry_count`, `instagram_status`, `facebook_status` 등

### 그대로 쓸 수 없는 부분 (수정 필요)
1. **영상이 공개 URL 전제**: 로컬 MP4를 직접 업로드하도록 바꾼다.
   - Instagram: resumable upload (`rupload.facebook.com`)로 바이너리 업로드 후 `media_publish`
   - Facebook: `Start`에서 받은 `upload_url`에 바이너리 전송 (템플릿 방식 유지, URL 대신 바이너리)
   - n8n에서는 `Read/Write Files from Disk` 노드로 읽는다. 자체 설치 n8n은 로컬 파일 접근이 가능하다.
   - 대안: Cloudinary 등 외부 호스팅 (비용, 외부 업로드 부담이 있어 비추천)
2. **시간대 Asia/Kolkata → Asia/Seoul** (필터 노드와 워크플로우 설정 둘 다)
3. **실패 알림 없음**: Error Trigger 워크플로우 + Telegram 또는 Gmail 알림 추가
4. **캡션 프롬프트**: 영상 URL만 넘기므로, SRT 내용을 넣고 한국어 + 공유/팔로우/유튜브 유입 CTA로 교체
5. **모델 노드**: 템플릿은 Gemini 모델명이 `models/gemini-3.8-flash`로 되어 있고 OpenRouter 노드도 있다. 실제 사용 가능한 Gemini 모델명으로 확인해서 교체하고, 안 쓰는 OpenRouter 노드는 정리한다.
6. **하루 3개**: 30분 주기 + 1건 처리 구조는 유지하되, 시트의 publish_date/publish_time에 하루 3개 슬롯(예: 12:00, 18:00, 21:00)을 배정해서 해결한다. 대량 예약은 시트 생성 스크립트로 처리한다.
7. **중복 게시 방지**: Mark Processing을 게시 전에 반드시 먼저 기록하고, 이미 IG 성공/FB 실패인 행은 성공한 쪽을 다시 게시하지 않도록 플랫폼별 상태로 분기한다. 게시 직후 `instagram_media_id`, `facebook_video_id`를 시트에 저장한다. `PROCESSING`이 오래 걸리면 자동 재게시 대신 알림 후 수동 확인으로 처리한다.

## 3. 구글 시트 설계 (`Queue` 탭)
`id | video_path | srt_path | title | publish_date | publish_time | status | instagram_status | facebook_status | instagram_media_id | facebook_video_id | retry_count | ig_caption | fb_caption | youtube_url | last_error | updated_at`
- status 값: PENDING / PROCESSING / POSTED / FAILED
- 캡션은 생성 후 시트에 저장해서, 재시도 시 다시 생성하지 않고 사용자가 검토/수정할 수 있게 한다.
- 대량 등록: 폴더의 MP4/SRT를 매칭해서 행을 만들고, 시작일부터 하루 3개씩 슬롯을 배정하는 스크립트(PowerShell 또는 Python)를 작성한다.

## 4. 캡션 방향
- 한국어, 영상 SRT 핵심 내용 기반
- 첫 줄은 훅, 핵심 요약 2~3줄
- CTA: 친구에게 공유, 팔로우, 유튜브 '운동과 건강함' 본편 시청 유도 (프로필 링크 안내)
- 해시태그 5~10개
- 의료적 효능을 단정하는 표현은 피한다 (건강 채널이므로 과장 금지)
- 인스타와 페이스북 캡션은 톤과 길이를 다르게 생성한다.

## 5. 구현 순서
1. 사용자 확인: MP4/SRT 폴더 위치, 파일명 규칙(MP4와 SRT가 같은 이름인지)
2. Node.js LTS + n8n 설치 (`npm i -g n8n` 또는 Docker), 시작 스크립트와 자동 시작(작업 스케줄러) 구성
   - 환경변수: `GENERIC_TIMEZONE=Asia/Seoul`, `TZ=Asia/Seoul`
   - 로컬 파일 접근 허용 설정 확인
3. n8n 공식 MCP 활성화: n8n 설정에서 Instance-level MCP 켜고 액세스 토큰 발급 → Claude Code에 연결
   - 안내 문서: https://docs.n8n.io/advanced-ai/mcp/accessing-n8n-mcp-server/ , https://n8n.io/mcp/
   - **정확한 연결 명령은 위 문서에서 확인하고 적용할 것** (이전 세션에서는 이 문서 본문을 읽지 못함)
4. 템플릿 import 후 위 2번의 수정 7가지 적용 (n8n UI 또는 MCP로)
5. 인증 (사용자 직접 수행, 어사이드 브라우저로 안내)
   - Google 로그인 (Sheets OAuth)
   - Gemini API 키
   - Meta 개발자 앱 생성, Instagram 프로페셔널 계정 + Facebook 페이지 연결, 권한 승인
     (예상 권한: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `business_management` 등. **현재 Meta 문서 기준으로 확인할 것**)
   - 장기 토큰 / 페이지 토큰 발급, IG Business Account ID와 FB Page ID 확인
   - 앱이 개발 모드면 본인 계정으로는 게시 가능하므로, 앱 검수 없이 진행 가능한지 확인한다.
6. 시트 생성(Google Sheets 커넥터 사용 가능) 및 대량 등록 스크립트
7. 실패 알림 워크플로우 연결
8. **테스트 (공개 게시 전 반드시 사용자 승인)**
   - 영상 1개로 캡션 생성만 먼저 실행해서 사용자와 검토
   - 사용자 승인 후 1개 실제 게시, IG/FB 양쪽 확인
   - 같은 행을 다시 실행해도 중복 게시되지 않는지 확인
   - 일부러 실패시켜 알림이 오는지 확인
9. 하루 3개 스케줄 활성화, 수백 개 등록

## 6. 주의 / 리스크
- Instagram 릴스 규격: MP4(H.264/AAC), 세로 9:16, 길이 제한, 파일 크기 제한을 확인한다. 규격에 안 맞으면 ffmpeg로 변환한다.
- Meta API 호출 한도(IG는 계정당 24시간 게시 한도 있음)를 확인한다. 하루 3개면 충분하다.
- n8n이 PC에서 돌아가므로, 예약 시간에 **PC가 켜져 있고 절전 모드가 아니어야** 한다. (한계로 사용자에게 안내하고, 필요하면 항상 켜진 환경 대안 논의)
- 토큰 만료: 장기 토큰은 약 60일이다. 갱신 방법이나 만료 알림을 설계한다.
- 비밀값(API 키, 토큰)은 n8n Credentials에만 저장하고 저장소/채팅에 남기지 않는다.
- 이전 세션에서 이 저장소(`opertel97-bit/-`)는 랜딩페이지 파일이 있는 별개 프로젝트다. 작업 파일은 `reels-automation/` 같은 하위 폴더에 두고 기존 파일은 건드리지 않는다.
