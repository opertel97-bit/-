# 유튜브 쇼츠 자동 예약

영상을 폴더에 넣거나 이미 올려 둔 비공개 쇼츠를 대상으로, **하루 정해진 시간(기본 09:00 / 15:00 / 21:00)에 하나씩 공개되도록** 유튜브에 예약을 걸어 주는 도구입니다.

- 제목, 설명, 태그 자동 입력 (`metadata.csv`)
- 설명란 아래 공통 문구 자동 추가 (`config.json`)
- 이미 예약된 시간은 피해서 다음 빈 시간에 배정
- 실행하면 먼저 **미리보기만** 보여 주고, `--apply`를 붙여야 실제로 바뀜
- 제목 중복, 태그 과다 같은 스팸 위험을 경고

> ⚠ **롱폼 연결(관련 동영상)은 유튜브 API로 설정할 수 없습니다.** 예약을 건 뒤 YouTube Studio에서 영상마다 직접 지정해 주세요.

---

## 1. 먼저 데모로 써 보기 (계정 연결 불필요)

```bash
cd shorts-scheduler
python shorts_scheduler.py existing --demo
```

```bash
python shorts_scheduler.py folder --demo
```

- `existing`: 이미 올라가 있는 비공개 쇼츠를 하루 3개씩 예약하는 계획
- `folder`: `videos/` 폴더의 새 영상을 업로드하면서 예약하는 계획

데모는 가짜 데이터를 쓰기 때문에 유튜브에는 아무 변화가 없습니다.

---

## 2. 내 채널에 연결하기 (처음 한 번만)

### 2-1. 파이썬 패키지 설치
```bash
pip install -r requirements.txt
```

### 2-2. Google Cloud 설정
1. https://console.cloud.google.com 에 접속해 **새 프로젝트**를 만듭니다.
2. **API 및 서비스 → 라이브러리**에서 **YouTube Data API v3**를 검색해 **사용**을 누릅니다.
3. **OAuth 동의 화면**을 설정합니다.
   - 사용자 유형: **외부**
   - 앱 이름과 이메일만 입력하면 됩니다.
   - **테스트 사용자**에 내 유튜브 채널의 구글 계정을 추가합니다.
4. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**
   - 애플리케이션 유형: **데스크톱 앱**
   - 만든 뒤 **JSON 다운로드** → 파일 이름을 `client_secret.json`으로 바꿔 이 폴더(`shorts-scheduler/`)에 넣습니다.

> `client_secret.json`과 `token.json`은 계정 비밀 정보입니다. `.gitignore`로 GitHub에 올라가지 않게 막아 두었습니다.

### 2-3. 첫 실행 (로그인)
```bash
python shorts_scheduler.py existing
```
브라우저가 열리면 유튜브 채널 계정으로 로그인하고 허용을 누릅니다. 그다음부터는 자동으로 로그인됩니다.

### 2-4. ⚠ API 감사 신청 (중요)
Google 정책상 **심사받지 않은 API 프로젝트로 올리거나 예약한 영상은 비공개로 고정될 수 있습니다.**
예약 시간이 와도 공개되지 않는다면 이 때문입니다.

- 신청서: https://support.google.com/youtube/contact/yt_api_form
- "개인 채널에 내 영상을 예약 업로드하는 용도"라고 적으면 됩니다. 며칠 걸릴 수 있습니다.

---

## 3. 실제로 쓰기

### 이미 올라간 비공개 쇼츠 예약
```bash
python shorts_scheduler.py existing
```
계획을 확인하고 괜찮으면 아래를 실행합니다.
```bash
python shorts_scheduler.py existing --apply
```
- 180초 이하 비공개 영상만 쇼츠로 봅니다. 기준은 `config.json`의 `shorts_max_seconds`에서 바꿉니다.
- 이미 예약이 잡힌 영상은 건드리지 않습니다.
- **한 번 공개됐다가 비공개로 돌린 영상은 유튜브 규칙상 예약이 안 됩니다.** 이런 영상은 ✘로 표시됩니다.

### 폴더의 새 영상 업로드 + 예약
1. 영상 파일을 `videos/` 폴더에 넣습니다.
2. `metadata.csv`에 파일명별로 제목, 설명, 태그를 적습니다. 엑셀로 편집해도 되고, 적지 않은 파일은 파일명이 제목이 됩니다.
3. 미리보기:
   ```bash
   python shorts_scheduler.py folder
   ```
4. 실제 업로드:
   ```bash
   python shorts_scheduler.py folder --apply
   ```
- 업로드가 끝난 영상은 `videos/done/`으로 옮겨집니다.
- 유튜브 업로드 한도 때문에 한 번에 최대 6개까지만 올립니다. 나머지는 다음 날 다시 실행하면 이어서 예약됩니다.
- 경고가 있으면 업로드를 멈춥니다. 무시하고 올리려면 `--force`를 붙입니다.

---

## 설정 바꾸기 (`config.json`)

| 항목 | 의미 | 기본값 |
|---|---|---|
| `daily_times` | 하루 공개 시각 목록 (개수 = 하루 업로드 수) | `["09:00","15:00","21:00"]` |
| `timezone` | 시간대 | `Asia/Seoul` |
| `start_date` | 이 날짜부터 배정 (`"2026-11-10"` 형식, 없으면 오늘부터) | `null` |
| `min_minutes_from_now` | 지금부터 최소 몇 분 뒤부터 배정할지 | `60` |
| `description_footer` | 모든 설명란 아래에 붙는 공통 문구 | 채널 소개 + 해시태그 |
| `category_id` | 카테고리 (15 = 반려동물/동물) | `"15"` |
| `made_for_kids` | 아동용 여부 | `false` |
| `max_uploads_per_run` | 한 번에 업로드할 최대 개수 | `6` |

예: 하루 2개(12시, 20시)로 바꾸려면 `"daily_times": ["12:00", "20:00"]`.
