#!/usr/bin/env python3
"""유튜브 쇼츠 자동 예약 도구.

사용법 (shorts-scheduler 폴더 안에서 실행):
  python shorts_scheduler.py existing --demo     # 가짜 데이터로 미리보기 (계정 연결 불필요)
  python shorts_scheduler.py folder   --demo     # videos/ 폴더 + metadata.csv 미리보기 (계정 연결 불필요)
  python shorts_scheduler.py existing            # 내 채널의 비공개 쇼츠 예약 계획 미리보기
  python shorts_scheduler.py existing --apply    # 위 계획을 실제로 적용
  python shorts_scheduler.py folder              # videos/ 폴더의 새 영상 업로드 계획 미리보기
  python shorts_scheduler.py folder   --apply    # 실제로 업로드 + 예약

--apply 를 붙이지 않으면 아무것도 바꾸지 않고 계획만 보여줍니다.
"""

import argparse
import csv
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
METADATA_PATH = BASE / "metadata.csv"
VIDEOS_DIR = BASE / "videos"
DONE_DIR = VIDEOS_DIR / "done"
CLIENT_SECRET = BASE / "client_secret.json"
TOKEN_PATH = BASE / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}


# ---------------------------------------------------------------- 설정 / 시간

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def next_free_slots(cfg, count, taken, now=None):
    """지금 이후의 예약 슬롯 중 이미 잡힌 시간(taken)을 피해서 count개를 돌려준다."""
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    min_start = now + timedelta(minutes=cfg.get("min_minutes_from_now", 60))
    times = sorted(datetime.strptime(t, "%H:%M").time() for t in cfg["daily_times"])
    taken_keys = {t.astimezone(tz).strftime("%Y-%m-%d %H:%M") for t in taken}

    day = now.date()
    if cfg.get("start_date"):
        day = max(day, datetime.strptime(cfg["start_date"], "%Y-%m-%d").date())

    slots = []
    while len(slots) < count:
        for t in times:
            slot = datetime.combine(day, t, tzinfo=tz)
            if slot < min_start or slot.strftime("%Y-%m-%d %H:%M") in taken_keys:
                continue
            slots.append(slot)
            if len(slots) == count:
                break
        day += timedelta(days=1)
    return slots


def to_rfc3339_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_duration(value):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


# ---------------------------------------------------------------- 메타데이터 / 안전 점검

def load_metadata():
    if not METADATA_PATH.exists():
        return {}
    with open(METADATA_PATH, encoding="utf-8-sig", newline="") as f:
        return {row["filename"].strip(): row for row in csv.DictReader(f) if row.get("filename")}


def build_snippet(cfg, title, description, tags):
    footer = cfg.get("description_footer", "")
    full_desc = description.strip()
    if footer:
        full_desc = (full_desc + "\n\n" + footer).strip()
    return {
        "title": title.strip(),
        "description": full_desc,
        "tags": [t.strip() for t in tags if t.strip()],
        "categoryId": str(cfg.get("category_id", "15")),
        "defaultLanguage": cfg.get("language", "ko"),
    }


def check_item(cfg, snippet, seen_titles):
    """유튜브 제한 및 스팸 정책에 걸릴 만한 부분을 경고로 돌려준다."""
    warns = []
    title, desc, tags = snippet["title"], snippet["description"], snippet["tags"]
    if not title:
        warns.append("제목이 비어 있음")
    if len(title) > 100:
        warns.append(f"제목이 100자를 넘음 ({len(title)}자)")
    if "<" in title or ">" in title or "<" in desc or ">" in desc:
        warns.append("제목/설명에 < > 문자는 쓸 수 없음")
    if len(desc) > 5000:
        warns.append(f"설명이 5000자를 넘음 ({len(desc)}자)")
    if sum(len(t) + 1 for t in tags) > 500:
        warns.append("태그 전체 길이가 500자를 넘음")
    if len(tags) > cfg.get("max_tags", 15):
        warns.append(f"태그가 너무 많음 ({len(tags)}개)")
    hashtags = re.findall(r"#\w+", title + " " + desc)
    if len(hashtags) > cfg.get("max_hashtags", 5):
        warns.append(f"해시태그가 너무 많음 ({len(hashtags)}개, 60개 넘으면 전부 무시됨)")
    key = re.sub(r"\W+", "", title.lower())
    if key and key in seen_titles:
        warns.append("다른 영상과 제목이 똑같음 (반복 콘텐츠로 보일 수 있음)")
    seen_titles.add(key)
    return warns


def print_plan(rows, tz_name):
    print()
    print(f"{'공개 예정 시각 (' + tz_name + ')':<26} | 영상")
    print("-" * 70)
    last_day = None
    for r in rows:
        day = r["slot"].strftime("%Y-%m-%d") + f" ({'월화수목금토일'[r['slot'].weekday()]})"
        label = day if day != last_day else " " * (len(day) + 1)  # 한글 요일은 2칸 폭
        last_day = day
        print(f"{label} {r['slot'].strftime('%H:%M')}   | {r['label']}")
        for w in r.get("warnings", []):
            print(f"{'':<26} |   ⚠ {w}")
    print("-" * 70)
    print(f"총 {len(rows)}개")


# ---------------------------------------------------------------- 유튜브 API

def youtube_client():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("필요한 패키지가 없습니다. 먼저 실행하세요:  pip install -r requirements.txt")

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                sys.exit("client_secret.json 이 없습니다. README의 'Google 설정' 단계를 먼저 해주세요.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def fetch_my_videos(yt):
    """내 채널 업로드 목록 전체 (비공개 포함)."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while True:
        resp = yt.playlistItems().list(
            part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token
        ).execute()
        ids += [it["contentDetails"]["videoId"] for it in resp["items"]]
        token = resp.get("nextPageToken")
        if not token:
            break
    videos = []
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(
            part="snippet,status,contentDetails", id=",".join(ids[i:i + 50])
        ).execute()
        videos += resp["items"]
    return videos


def scheduled_times(videos):
    out = []
    for v in videos:
        p = v["status"].get("publishAt")
        if p:
            out.append(datetime.fromisoformat(p.replace("Z", "+00:00")))
    return out


# ---------------------------------------------------------------- 데모용 가짜 데이터

def demo_videos():
    titles = [
        "퇴근한 집사를 맞이하는 고양이의 자세",
        "츄르 앞에서 이성을 잃는 순간",
        "새벽 4시 우다다 현장 포착",
        "박스만 보면 들어가야 하는 이유",
        "집사 키보드를 점령한 고양이",
        "목욕 소리만 듣고 숨어버린 냥이",
        "창밖 새 구경하다 채터링 터진 날",
        "엄마 고양이가 아기 혼내는 법",
    ]
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    vids = []
    for i, t in enumerate(titles):
        vids.append({
            "id": f"DEMO{i + 1:07d}",
            "snippet": {"title": t, "publishedAt": (base + timedelta(days=i)).isoformat()},
            "status": {"privacyStatus": "private"},
            "contentDetails": {"duration": f"PT{20 + i * 3}S"},
        })
    # 이미 예약된 영상 1개 (이 시간은 건너뛰고 배정되는지 보여주기 위함)
    vids.append({
        "id": "DEMO0000099",
        "snippet": {"title": "이미 예약돼 있던 쇼츠", "publishedAt": base.isoformat()},
        "status": {"privacyStatus": "private", "publishAt": None},
        "contentDetails": {"duration": "PT40S"},
    })
    # 롱폼 영상 1개 (쇼츠가 아니라서 제외되는지 보여주기 위함)
    vids.append({
        "id": "DEMO0000100",
        "snippet": {"title": "[롱폼] 우리 고양이 1년 성장 기록", "publishedAt": base.isoformat()},
        "status": {"privacyStatus": "private"},
        "contentDetails": {"duration": "PT12M30S"},
    })
    return vids


# ---------------------------------------------------------------- 명령: existing

def cmd_existing(cfg, args):
    tz = ZoneInfo(cfg["timezone"])
    if args.demo:
        videos = demo_videos()
        first_slot = next_free_slots(cfg, 1, [])[0]
        videos[-2]["status"]["publishAt"] = to_rfc3339_utc(first_slot)
        yt = None
        print("[데모 모드] 가짜 채널 데이터로 보여줍니다. 실제 유튜브에는 아무것도 바뀌지 않습니다.")
    else:
        yt = youtube_client()
        videos = fetch_my_videos(yt)

    max_sec = cfg.get("shorts_max_seconds", 180)
    targets = [
        v for v in videos
        if v["status"]["privacyStatus"] == "private"
        and not v["status"].get("publishAt")
        and parse_iso_duration(v["contentDetails"]["duration"]) <= max_sec
    ]
    targets.sort(key=lambda v: v["snippet"]["publishedAt"])
    skipped_long = sum(
        1 for v in videos
        if v["status"]["privacyStatus"] == "private"
        and parse_iso_duration(v["contentDetails"]["duration"]) > max_sec
    )

    if not targets:
        print("예약할 비공개 쇼츠가 없습니다.")
        return

    slots = next_free_slots(cfg, len(targets), scheduled_times(videos))
    rows = [
        {"slot": s, "video": v, "label": f"{v['snippet']['title']}  ({v['id']})"}
        for s, v in zip(slots, targets)
    ]
    print_plan(rows, cfg["timezone"])
    already = len(scheduled_times(videos))
    if already:
        print(f"이미 예약돼 있는 영상 {already}개는 그대로 두고, 그 시간은 피해서 배정했습니다.")
    if skipped_long:
        print(f"{max_sec}초가 넘는 비공개 영상 {skipped_long}개는 쇼츠가 아니라서 제외했습니다.")

    if not args.apply:
        print("\n미리보기만 했습니다. 실제로 적용하려면 --apply 를 붙여 다시 실행하세요.")
        return
    if args.demo:
        print("\n[데모 모드] --apply 는 실제 계정 연결 후에만 동작합니다.")
        return

    ok = 0
    for r in rows:
        v = r["video"]
        # part=status 업데이트는 빠진 항목을 기본값으로 되돌리므로 기존 값을 그대로 유지한다
        keep = ("license", "embeddable", "publicStatsViewable",
                "selfDeclaredMadeForKids", "containsSyntheticMedia")
        status = {k: v["status"][k] for k in keep if k in v["status"]}
        status.setdefault("selfDeclaredMadeForKids", cfg.get("made_for_kids", False))
        status.update(privacyStatus="private", publishAt=to_rfc3339_utc(r["slot"]))
        body = {"id": v["id"], "status": status}
        try:
            yt.videos().update(part="status", body=body).execute()
            ok += 1
            print(f"  ✔ {r['slot'].astimezone(tz):%m/%d %H:%M}  {v['snippet']['title']}")
        except Exception as e:  # 예: 한 번 공개됐던 영상은 예약 불가
            print(f"  ✘ {v['snippet']['title']}: {e}")
    print(f"\n{ok}/{len(rows)}개 예약 완료. YouTube Studio > 콘텐츠 > '예약됨'에서 확인하세요.")


# ---------------------------------------------------------------- 명령: folder

def cmd_folder(cfg, args):
    tz = ZoneInfo(cfg["timezone"])
    VIDEOS_DIR.mkdir(exist_ok=True)
    files = sorted(p for p in VIDEOS_DIR.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    meta = load_metadata()

    if args.demo and not files:
        files = [VIDEOS_DIR / name for name in meta]  # 파일이 없으면 metadata.csv 목록으로 시연
        print("[데모 모드] videos/ 폴더가 비어 있어 metadata.csv 에 적힌 파일명으로 보여줍니다.")
    elif args.demo:
        print("[데모 모드] 유튜브에 연결하지 않고 계획만 보여줍니다.")

    if not files:
        print("videos/ 폴더에 업로드할 영상이 없습니다.")
        return

    per_run = cfg.get("max_uploads_per_run", 6)
    if len(files) > per_run:
        print(f"영상이 {len(files)}개라 이번에는 {per_run}개만 올립니다 (유튜브 하루 업로드 한도). "
              "나머지는 내일 다시 실행하면 이어서 올라갑니다.")
        files = files[:per_run]

    yt = None
    taken = []
    if not args.demo:
        yt = youtube_client()
        taken = scheduled_times(fetch_my_videos(yt))

    slots = next_free_slots(cfg, len(files), taken)
    seen = set()
    rows = []
    for slot, path in zip(slots, files):
        m = meta.get(path.name)
        if m:
            tags = re.split(r"[,|]", m.get("tags", ""))
            snippet = build_snippet(cfg, m.get("title", ""), m.get("description", ""), tags)
        else:
            snippet = build_snippet(cfg, path.stem, "", [])
        warns = check_item(cfg, snippet, seen)
        if not m:
            warns.insert(0, "metadata.csv 에 없음 → 파일명을 제목으로 사용")
        rows.append({"slot": slot, "path": path, "snippet": snippet, "warnings": warns,
                     "label": f"{snippet['title']}  [{path.name}]"})

    print_plan(rows, cfg["timezone"])
    if rows:
        s = rows[0]["snippet"]
        print("\n첫 번째 영상에 들어갈 내용 예시")
        print(f"  제목: {s['title']}")
        print(f"  태그: {', '.join(s['tags']) or '(없음)'}")
        print("  설명:")
        for line in s["description"].splitlines():
            print(f"    {line}")

    if not args.apply:
        print("\n미리보기만 했습니다. 실제로 업로드하려면 --apply 를 붙여 다시 실행하세요.")
        return
    if args.demo:
        print("\n[데모 모드] --apply 는 실제 계정 연결 후에만 동작합니다.")
        return
    if any(r["warnings"] for r in rows) and not args.force:
        sys.exit("\n⚠ 경고가 있는 영상이 있어 멈췄습니다. 고친 뒤 다시 실행하거나, 무시하려면 --force 를 붙이세요.")

    from googleapiclient.http import MediaFileUpload

    DONE_DIR.mkdir(exist_ok=True)
    for r in rows:
        body = {
            "snippet": r["snippet"],
            "status": {
                "privacyStatus": "private",
                "publishAt": to_rfc3339_utc(r["slot"]),
                "selfDeclaredMadeForKids": cfg.get("made_for_kids", False),
            },
        }
        media = MediaFileUpload(str(r["path"]), chunksize=-1, resumable=True)
        try:
            req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
            shutil.move(str(r["path"]), DONE_DIR / r["path"].name)
            print(f"  ✔ {r['slot'].astimezone(tz):%m/%d %H:%M}  {r['snippet']['title']}  "
                  f"https://youtube.com/shorts/{resp['id']}")
        except Exception as e:
            print(f"  ✘ {r['path'].name}: {e}")
            if "quotaExceeded" in str(e):
                print("    오늘 업로드 한도를 다 썼습니다. 내일 다시 실행하세요.")
                break
    print("\n롱폼 연결(관련 동영상)은 YouTube Studio에서 영상마다 직접 지정해 주세요.")


def main():
    ap = argparse.ArgumentParser(description="유튜브 쇼츠 자동 예약")
    ap.add_argument("mode", choices=["existing", "folder"],
                    help="existing: 이미 올라간 비공개 쇼츠 예약 / folder: videos 폴더의 새 영상 업로드+예약")
    ap.add_argument("--apply", action="store_true", help="미리보기가 아니라 실제로 적용")
    ap.add_argument("--demo", action="store_true", help="계정 연결 없이 가짜 데이터로 시연")
    ap.add_argument("--force", action="store_true", help="경고가 있어도 업로드 진행")
    args = ap.parse_args()
    cfg = load_config()
    (cmd_existing if args.mode == "existing" else cmd_folder)(cfg, args)


if __name__ == "__main__":
    main()
