# 나만알고양 — 고양이밈 제작 가이드 PDF 랜딩페이지

빌드 과정이 없는 순수 HTML/CSS/JS 정적 페이지입니다. 서버 비용 없이 Vercel 무료 플랜으로 바로 배포할 수 있습니다.

## 파일 구성

- `index.html` — 페이지 전체 구조 및 문구
- `styles.css` — 디자인 (에디토리얼 + 고양이 톤)
- `script.js` — 구매 신청 폼 버튼 등 소소한 동작

## 꼭 채워야 할 부분 ( `[ ]` 로 표시된 곳 )

- `#buy` 가격 (`price-now`, `price-old`)
- `#howtobuy` 계좌번호 / 예금주
- 구매 신청 폼 링크 (`#form-link` 의 `href`, 구글폼으로 교체)
- 발송 소요 시간 / 환불 정책 문구
- 문의처 (footer의 카카오톡 또는 이메일)
- 채널 실적 수치 (`.proof-grid` 안 구독자 수, 조회수 등)
- 후기 (`.testi-grid`)

## 로컬에서 확인하기

파일을 브라우저로 바로 열거나, 아래처럼 간단히 로컬 서버를 띄워 확인할 수 있습니다.

```bash
npx serve .
```

## Vercel로 배포하기 (무료)

1. 이 저장소를 GitHub에 push
2. https://vercel.com 에서 New Project → 이 저장소 선택
3. Framework Preset: **Other** (정적 파일이라 별도 빌드 명령 불필요)
4. Deploy 클릭

배포 후에도 `index.html` / `styles.css`를 수정해서 다시 push하면 자동으로 반영됩니다.
