#!/usr/bin/env python3
"""유튜브 쇼츠 예약 · 제목 · 설명란(해시태그) 자동화 도구.

공식 YouTube Data API v3 만 사용합니다 (브라우저 자동 클릭 없음).
영상 삭제, 재업로드, 즉시 공개 전환은 이 도구로는 절대 일어나지 않습니다.

  python yt.py auth        구글 계정 연결 (최초 1회)
  python yt.py fetch       내 채널의 비공개 쇼츠 목록을 plan.csv 로 가져오기
  python yt.py generate    Claude 로 제목 · 설명 · 해시태그 · 태그 초안 채우기
  python yt.py schedule    예약 시간 자동 배정 (예: 매일 18:00, 20:00, 22:00)
  python yt.py check       plan.csv 검사 (글자 수, 시간 중복, 지난 시간 등)
  python yt.py apply       변경 내용 미리보기 (--execute 를 붙여야 실제 반영)
  python yt.py verify      유튜브에 실제로 반영됐는지 다시 확인
  python yt.py linklist    스튜디오에서 직접 연결할 '관련 동영상' 체크리스트 만들기
  python yt.py calendar    롱폼 · 쇼츠 예약 달력을 브라우저로 보기
  python yt.py export      해시태그 없는 영상 목록 → Claude 대화창에 붙여넣기용 (무료)
  python yt.py import      Claude 가 준 해시태그(tags.txt)를 plan.csv 에 채우기
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE = Path(__file__).resolve().parent

# plan.csv 열 순서. current_* 는 fetch 가 유튜브에서 읽어온 값(덮어씀),
# 나머지는 사용자가 엑셀로 편집하는 값(fetch 가 건드리지 않음).
COLUMNS = [
    "video_id",
    "current_title",
    "current_privacy",
    "current_publish_at",
    "duration_sec",
    "is_short",
    "memo",
    "related_longform",
    "new_title",
    "new_description",
    "new_tags",
    "publish_at",
    "apply_status",
    "studio_link",
]
USER_COLUMNS = {"memo", "related_longform", "new_title", "new_description", "new_tags", "publish_at", "apply_status"}

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
HASHTAG_RECOMMENDED_MAX = 15
HASHTAG_HARD_MAX = 60
MIN_LEAD = timedelta(minutes=15)  # 지금부터 최소 이만큼 뒤여야 예약 가능

QUOTA_UPDATE = 50  # videos.update 1회 비용 (기본 할당량: 하루 10,000)

HASHTAG_RE = re.compile(r"(?<![\w#])#([^\s#]+)")


class UserError(Exception):
    """사용자에게 그대로 보여줄 오류."""


# ---------------------------------------------------------------- 설정 / 파일


def load_config() -> dict:
    path = BASE / "config.yaml"
    if not path.exists():
        example = BASE / "config.example.yaml"
        if not example.exists():
            raise UserError("config.yaml 이 없습니다.")
        print("※ config.yaml 이 없어 config.example.yaml 을 사용합니다. 복사해서 내 채널에 맞게 고쳐주세요.")
        path = example
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("timezone", "Asia/Seoul")
    cfg.setdefault("plan_file", "plan.csv")
    cfg.setdefault("shorts_max_seconds", 180)
    cfg.setdefault("client_secret_file", "client_secret.json")
    cfg.setdefault("token_file", "token.json")
    cfg.setdefault("schedule", {})
    cfg.setdefault("channel", {})
    cfg.setdefault("writing", {})
    cfg.setdefault("claude", {})
    return cfg


def tz_of(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["timezone"])


def resolve(cfg: dict, key: str) -> Path:
    p = Path(cfg[key])
    return p if p.is_absolute() else BASE / p


def load_plan(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = []
        for raw in csv.DictReader(f):
            rows.append({c: (raw.get(c) or "").strip() for c in COLUMNS})
        return rows


def save_plan(rows: list[dict], path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    # utf-8-sig: 윈도우 엑셀에서 한글이 깨지지 않도록 BOM 포함
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    try:
        os.replace(tmp, path)
    except PermissionError:
        raise UserError(f"{path.name} 을 저장할 수 없습니다. 엑셀에서 파일을 닫고 다시 실행해주세요. (임시 저장본: {tmp.name})")


# ---------------------------------------------------------------- 시간 / 값 변환


def parse_local(text: str, tz: ZoneInfo) -> datetime:
    """'2026-11-10 18:00' 같은 한국 시간 문자열 -> 시간대 포함 datetime."""
    s = text.strip().replace("/", "-").replace(".", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    raise ValueError(f"시간 형식을 알 수 없음: {text!r} (예: 2026-11-10 18:00)")


def fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def parse_api_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_api_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_duration_seconds(d: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", d or "")
    if not m:
        return 0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return ((days * 24 + h) * 60 + mi) * 60 + s


def split_tags(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n]", text or "") if t.strip()]


def tags_length(tags: list[str]) -> int:
    # 유튜브는 공백이 있는 태그를 따옴표로 감싸 계산하고, 쉼표도 글자 수에 포함
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)


def natural_key(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s or "")]


def dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x.lower()
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def normalize_hashtag(h: str) -> str:
    h = re.sub(r"\s+", "", h.strip()).lstrip("#")
    return f"#{h}" if h else ""


# ---------------------------------------------------------------- 유튜브 API


def get_service(cfg: dict, interactive: bool = True):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise UserError("구글 라이브러리가 없습니다. 먼저 `pip install -r requirements.txt` 를 실행해주세요.")

    scopes = ["https://www.googleapis.com/auth/youtube"]
    token_path = resolve(cfg, "token_file")
    secret_path = resolve(cfg, "client_secret_file")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not interactive:
                raise UserError("구글 로그인이 필요합니다. `python yt.py auth` 를 먼저 실행해주세요.")
            if not secret_path.exists():
                raise UserError(f"{secret_path.name} 이 없습니다. README 의 'Google Cloud 설정' 을 따라 받은 파일을 이 폴더에 두세요.")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def api_error_text(e: Exception) -> tuple[str, str]:
    """HttpError -> (reason, message)."""
    content = getattr(e, "content", b"")
    try:
        err = json.loads(content.decode("utf-8"))["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        return reason, err.get("message", str(e))
    except Exception:
        return "", str(e)


def list_upload_ids(yt) -> tuple[str, list[str]]:
    ch = yt.channels().list(part="snippet,contentDetails", mine=True).execute()
    items = ch.get("items") or []
    if not items:
        raise UserError("이 구글 계정에 연결된 유튜브 채널을 찾지 못했습니다. 브랜드 계정이면 로그인 시 해당 채널을 선택해야 합니다.")
    channel_title = items[0]["snippet"]["title"]
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while True:
        resp = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token).execute()
        ids += [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    ids = list(dict.fromkeys(ids))  # 업로드 목록이 같은 영상을 중복으로 줄 때가 있음
    # 업로드 목록은 최근 예약 · 비공개 영상을 빠뜨리는 경우가 있어 '내 영상 검색'으로 보충
    try:
        ids += search_my_video_ids(yt, set(ids))
    except Exception as e:  # 검색이 실패해도 업로드 목록만으로 계속 진행
        reason, msg = api_error_text(e)
        print(f"※ 최근 영상 보충 검색 실패 ({reason or msg}). 최근 예약 영상 일부가 빠질 수 있습니다.")
    return channel_title, ids


SEARCH_MAX_PAGES = 10  # 페이지당 할당량 100


def search_my_video_ids(yt, known: set[str]) -> list[str]:
    """최근 영상부터 검색해 업로드 목록에 없는 영상 ID 를 찾음. 새 영상이 안 나오는 페이지가 나오면 멈춤."""
    extra, token = [], None
    for _ in range(SEARCH_MAX_PAGES):
        resp = yt.search().list(part="id", forMine=True, type="video", order="date", maxResults=50, pageToken=token).execute()
        page = [it["id"]["videoId"] for it in resp.get("items", []) if it.get("id", {}).get("videoId")]
        new = [i for i in page if i not in known]
        extra += new
        known.update(new)
        token = resp.get("nextPageToken")
        if not new or not token:
            break
    return extra


def get_videos(yt, ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        resp = yt.videos().list(part="snippet,status,contentDetails", id=",".join(chunk), maxResults=50).execute()
        for v in resp.get("items", []):
            out[v["id"]] = v
    return out


# ---------------------------------------------------------------- 명령: auth / fetch


def cmd_auth(cfg, args):
    yt = get_service(cfg)
    title, ids = list_upload_ids(yt)
    print(f"연결 완료: 채널 '{title}' (업로드 {len(ids)}개 확인)")


def cmd_fetch(cfg, args):
    tz = tz_of(cfg)
    yt = get_service(cfg, interactive=True)
    channel_title, ids = list_upload_ids(yt)
    videos = get_videos(yt, ids)
    plan_path = resolve(cfg, "plan_file")
    rows = load_plan(plan_path)
    by_id = {r["video_id"]: r for r in rows}
    max_short = int(cfg["shorts_max_seconds"])

    counts = {"private": 0, "unlisted": 0, "public": 0, "scheduled": 0}
    new_rows = []
    for vid, v in videos.items():
        st, sn = v.get("status", {}), v.get("snippet", {})
        privacy = st.get("privacyStatus", "")
        counts[privacy] = counts.get(privacy, 0) + 1
        if st.get("publishAt"):
            counts["scheduled"] += 1
        dur = iso_duration_seconds(v.get("contentDetails", {}).get("duration", ""))
        # 처리 중인 영상은 길이가 0으로 나올 수 있어 일단 쇼츠 후보로 둠
        is_short = dur <= max_short
        if not args.all_types and not is_short:
            continue
        if not args.include_public and privacy != "private":
            continue
        current = {
            "video_id": vid,
            "current_title": sn.get("title", ""),
            "current_privacy": privacy,
            "current_publish_at": fmt_local(parse_api_time(st["publishAt"]), tz) if st.get("publishAt") else "",
            "duration_sec": str(dur),
            "is_short": "Y" if is_short else "N",
            "studio_link": f"https://studio.youtube.com/video/{vid}/edit",
        }
        if vid in by_id:
            by_id[vid].update(current)  # 사용자가 편집한 열은 그대로 유지
        else:
            row = {c: "" for c in COLUMNS}
            row.update(current)
            row["publish_at"] = current["current_publish_at"]  # 이미 예약된 시간은 그대로 가져옴
            new_rows.append(row)

    new_rows.sort(key=lambda r: natural_key(r["current_title"]))
    rows += new_rows
    save_plan(rows, plan_path)

    cache = resolve(cfg, "plan_file").with_name("videos_cache.json")
    cache.write_text(json.dumps(videos, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"채널 '{channel_title}': 업로드 {len(ids)}개 · 비공개 {counts['private']} · 일부공개 {counts.get('unlisted', 0)} · 공개 {counts.get('public', 0)} · 예약됨 {counts['scheduled']}")
    print(f"plan.csv: 새로 추가 {len(new_rows)}개, 전체 {len(rows)}개 → {plan_path}")
    if not rows:
        print("※ 대상 영상이 없습니다. 스튜디오의 '초안' 영상은 API 에 안 보일 수 있으니, 스튜디오에서 초안을 열어 '비공개'로 저장한 뒤 다시 fetch 해보세요.")


# ---------------------------------------------------------------- 명령: schedule


def iter_slots(start: date, times: list[time], tz: ZoneInfo):
    d = start
    while True:
        for t in times:
            yield datetime.combine(d, t, tzinfo=tz)
        d += timedelta(days=1)


def parse_times(text: str) -> list[time]:
    out = []
    for part in re.split(r"[,\s]+", text.strip()):
        if part:
            h, m = part.split(":")
            out.append(time(int(h), int(m)))
    if not out:
        raise UserError("--times 가 비었습니다. 예: --times 18:00,20:00,22:00")
    return sorted(set(out))


def plan_schedule(rows, start: date, times: list[time], tz, now: datetime, overwrite=False, include_long=False, limit=None):
    """비어 있는 publish_at 에 슬롯을 순서대로 배정. 바뀐 (row, 값) 목록 반환."""
    targets = [
        r for r in rows
        if r["current_privacy"] == "private"
        and (include_long or r["is_short"] == "Y")
        and (overwrite or not r["publish_at"])
    ]
    if limit:
        targets = targets[:limit]
    target_ids = {r["video_id"] for r in targets}
    taken = set()
    for r in rows:
        if r["video_id"] not in target_ids and r["publish_at"]:
            try:
                taken.add(parse_local(r["publish_at"], tz))
            except ValueError:
                pass
    slots = (s for s in iter_slots(start, times, tz) if s >= now + MIN_LEAD and s not in taken)
    changes = []
    for r, slot in zip(targets, slots):
        value = fmt_local(slot, tz)
        if r["publish_at"] != value:
            changes.append((r, r["publish_at"], value))
        r["publish_at"] = value
    return changes


def cmd_schedule(cfg, args):
    tz = tz_of(cfg)
    plan_path = resolve(cfg, "plan_file")
    rows = load_plan(plan_path)
    if not rows:
        raise UserError("plan.csv 가 비었습니다. 먼저 `python yt.py fetch` 를 실행해주세요.")
    times = parse_times(args.times or ",".join(cfg["schedule"].get("times") or ["18:00", "20:00", "22:00"]))
    now = datetime.now(tz)
    start = date.fromisoformat(args.start) if args.start else (now + MIN_LEAD).date()
    changes = plan_schedule(rows, start, times, tz, now, overwrite=args.overwrite, include_long=args.include_long, limit=args.limit)
    if not changes:
        print("새로 배정할 영상이 없습니다. (이미 시간이 있는 영상은 --overwrite 를 붙여야 다시 배정합니다)")
        return
    for r, old, new in changes:
        arrow = f"{old} → {new}" if old else new
        print(f"  {arrow}   {r['new_title'] or r['current_title']}")
    save_plan(rows, plan_path)
    print(f"{len(changes)}개 예약 시간 배정 완료 (아직 유튜브에는 반영 안 됨 → `python yt.py apply` 로 확인)")


# ---------------------------------------------------------------- 명령: generate (Claude)


def wants_title(cfg: dict) -> bool:
    # 기본값 False: 제목은 건드리지 않고 설명란 · 해시태그 · 태그만 작성
    return bool(cfg["writing"].get("generate_title", False))


def gen_schema(cfg: dict) -> dict:
    props = {
        "video_id": {"type": "string"},
        "target_viewer": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    if wants_title(cfg):
        props["title"] = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": props,
                    "required": list(props),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def build_system_prompt(cfg: dict) -> str:
    ch, wr = cfg["channel"], cfg["writing"]
    n_hash = wr.get("max_generated_hashtags", 4)
    n_tags = wr.get("max_generated_tags", 10)
    fixed = " ".join(normalize_hashtag(h) for h in (wr.get("fixed_hashtags") or []))
    lines = [
        "너는 유튜브 쇼츠의 노출·검색 담당자야. 목표는 각 영상이 '이 내용이 꼭 필요한 시청자'에게 추천·검색되도록",
        "설명란 해시태그와 검색 태그를 고르는 것이야. 한국어로 작성해.",
        "",
        f"채널 이름: {ch.get('name', '')}",
        f"채널 소개: {ch.get('about', '')}",
        f"말투/톤: {ch.get('tone', '')}",
        "",
        "각 영상마다 이렇게 생각해:",
        "1. target_viewer: 제목과 기존 설명으로 영상의 핵심 주제를 파악하고, 이 영상이 꼭 필요한 시청자를 한 줄로 적어.",
        "   (예: '최근 소화불량과 등 통증이 있는 50~60대', '무릎이 아파 운동을 망설이는 중년')",
        "2. 그 시청자가 유튜브 검색창에 실제로 칠 법한 말을 떠올려.",
        "",
        f"hashtags (설명란 맨 아래에 붙는 해시태그, {n_hash}개 이하, 중요한 순서대로):",
        "- 1번: 영상의 핵심 주제 키워드 (질환명·부위·운동명 등 가장 구체적인 것)",
        "- 2번: 시청자의 증상·고민·상황을 담은 키워드",
        "- 나머지: 주제가 속한 분야 키워드 1개 (예: 건강정보, 운동법). 너무 넓은 단어만으로 채우지 마.",
        "- 사람들이 실제로 검색하는 흔한 표기를 써. 억지로 만든 합성어 금지. 띄어쓰기 없이.",
        "- 영상과 관계없는 유행 해시태그나 오해를 부르는 해시태그 금지 (유튜브 정책 위반).",
        f"- {fixed} 는 프로그램이 따로 붙이니 넣지 마." if fixed else "",
        "",
        f"tags (설명란에 안 보이는 검색용 태그, {n_tags}개 이하):",
        "- 핵심 키워드, 동의어·다른 표기(예: 췌장암/췌장 암), 검색 문구(예: 췌장암 초기증상, 췌장암 원인)를 섞어.",
        "- 영상에 나온 전문가 이름이나 프로그램명이 제목·설명에 있으면 포함해도 돼.",
        "",
        "description (설명 본문):",
        "- 2~3문장. 이 영상에서 무엇을 알 수 있는지 핵심 키워드를 자연스럽게 넣어 써.",
        "- 해시태그, 링크, 채널 고정 문구는 넣지 마 (프로그램이 따로 붙임).",
        "",
        "공통 규칙:",
        "- 제목·기존 설명·메모·관련 롱폼에 없는 사실을 지어내지 마. 과장이나 단정적인 효과 주장 금지.",
        "- 꺾쇠괄호(< >) 사용 금지.",
        "- 입력으로 받은 video_id 를 그대로 돌려줘.",
    ]
    if wants_title(cfg):
        lines += [
            f"- title: 새 제목 {wr.get('title_max_chars', 40)}자 이내. 첫 몇 글자에 핵심 궁금증이나 이득이 드러나게.",
        ]
    if ch.get("extra_rules"):
        lines += [f"- {x}" for x in ch["extra_rules"]]
    examples = [t for t in (ch.get("title_examples") or []) if t and not t.startswith("예시 제목")]
    if wants_title(cfg) and examples:
        lines += ["", "이 채널에서 반응이 좋았던 제목 예시 (스타일만 참고):"] + [f"- {t}" for t in examples]
    return "\n".join(l for l in lines if l is not None)


def compose_description(body: str, hashtags: list[str], cfg: dict) -> str:
    """본문 + 고정 문구 + 해시태그. 본문에 이미 있는 문구·해시태그는 중복으로 붙이지 않음."""
    wr = cfg["writing"]
    limit = int(wr.get("max_generated_hashtags", 4))
    body = body.strip()
    already = {h.lower() for h in HASHTAG_RE.findall(body)}
    tags = [t for t in dedupe(
        [normalize_hashtag(h) for h in hashtags[:limit]] + [normalize_hashtag(h) for h in (wr.get("fixed_hashtags") or [])]
    ) if t and t[1:].lower() not in already]
    parts = [body]
    footer = (wr.get("description_footer") or "").strip()
    if footer and footer not in body:
        parts.append(footer)
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(p for p in parts if p)


def compose_tags(tags: list[str], cfg: dict) -> str:
    wr = cfg["writing"]
    limit = int(wr.get("max_generated_tags", 10))
    merged = dedupe([t.strip().lstrip("#") for t in (wr.get("default_tags") or []) + tags[:limit]])
    while merged and tags_length(merged) > TAGS_MAX_CHARS:
        merged.pop()
    return ", ".join(merged)


def call_claude(client, cfg: dict, items: list[dict]) -> list[dict]:
    cl = cfg["claude"]
    what = "제목·설명·해시태그·태그" if wants_title(cfg) else "설명·해시태그·태그 (제목은 그대로 둠)"
    resp = client.beta.messages.create(
        model=cl.get("model", "claude-opus-5-5"),
        max_tokens=16000,
        system=build_system_prompt(cfg),
        messages=[{"role": "user", "content": f"아래 영상들의 {what}를 써줘.\n\n" + json.dumps(items, ensure_ascii=False, indent=1)}],
        output_config={"effort": cl.get("effort", "medium"), "format": {"type": "json_schema", "schema": gen_schema(cfg)}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # 안전 분류기가 거절하면 서버가 다른 모델로 자동 재시도
    )
    if resp.stop_reason == "refusal":
        raise UserError("Claude 가 이 묶음 작성을 거절했습니다. 제목·메모 내용을 확인해주세요.")
    if resp.stop_reason == "max_tokens":
        raise UserError("응답이 잘렸습니다. config.yaml 의 claude.batch_size 를 줄여주세요.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)["items"]


def cmd_generate(cfg, args):
    try:
        import anthropic
    except ImportError:
        raise UserError("anthropic 라이브러리가 없습니다. `pip install -r requirements.txt` 를 실행해주세요.")
    plan_path = resolve(cfg, "plan_file")
    rows = load_plan(plan_path)
    targets = [
        r for r in rows
        if (args.include_long or r["is_short"] == "Y")
        and r["current_privacy"] != "public"
        and (args.overwrite or not r["new_description"])
    ]
    if args.only:
        wanted = set(split_tags(args.only))
        targets = [r for r in targets if r["video_id"] in wanted]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("작성할 영상이 없습니다. (이미 new_description 이 있는 영상은 --overwrite 를 붙여야 다시 씁니다)")
        return

    cache_path = plan_path.with_name("videos_cache.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    keep_body = cfg["writing"].get("keep_existing_description", True)

    client = anthropic.Anthropic()
    batch = int(cfg["claude"].get("batch_size", 10))
    done = 0
    for i in range(0, len(targets), batch):
        chunk = targets[i : i + batch]
        items, old = [], {}
        for r in chunk:
            old[r["video_id"]] = (cache.get(r["video_id"], {}).get("snippet", {}).get("description") or "").strip()
            items.append({
                "video_id": r["video_id"],
                "title": r["current_title"],
                "memo": r["memo"],
                "related_longform": r["related_longform"],
                "existing_description": old[r["video_id"]][:1500],
            })
        try:
            results = call_claude(client, cfg, items)
        except anthropic.AuthenticationError:
            raise UserError("Anthropic API 키가 없거나 잘못됐습니다. 환경변수 ANTHROPIC_API_KEY 를 설정해주세요.")
        except anthropic.RateLimitError:
            print("요청 한도 초과. 잠시 후 다시 실행하면 남은 영상부터 이어서 작성합니다.")
            break
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            print(f"일시적 오류로 중단: {e}. 다시 실행하면 이어서 작성합니다.")
            break
        by_id = {it["video_id"]: it for it in results}
        for r in chunk:
            it = by_id.get(r["video_id"])
            if not it:
                print(f"  ! 결과 누락: {r['current_title']}")
                continue
            if wants_title(cfg):
                r["new_title"] = it["title"].strip()
            # 이미 설명을 써둔 영상은 그 글을 살리고 해시태그만 붙임
            body = old[r["video_id"]] if (keep_body and old[r["video_id"]]) else it["description"]
            r["new_description"] = compose_description(body, it["hashtags"], cfg)
            r["new_tags"] = compose_tags(it["tags"], cfg)
            done += 1
            print(f"  ✓ {r['current_title']}")
            print(f"      대상: {it.get('target_viewer', '')}")
            print(f"      해시태그: {' '.join('#' + h for h in HASHTAG_RE.findall(r['new_description']))}")
        save_plan(rows, plan_path)  # 묶음마다 저장 (중간에 끊겨도 보존)
    print(f"{done}개 작성 완료. plan.csv 를 열어 확인·수정한 뒤 `python yt.py check` 를 실행하세요.")


# ---------------------------------------------------------------- 명령: check


def check_rows(rows: list[dict], tz: ZoneInfo, now: datetime) -> list[tuple[dict, str, str]]:
    """(row, 'ERROR'|'WARN', 메시지) 목록. ERROR 가 있는 행은 apply 에서 건너뜀."""
    problems = []
    seen_times: dict[datetime, str] = {}
    for r in rows:
        def add(level, msg):
            problems.append((r, level, msg))

        t = r["new_title"]
        if t:
            if len(t) > TITLE_MAX:
                add("ERROR", f"제목이 {len(t)}자 (최대 {TITLE_MAX}자)")
            if "<" in t or ">" in t:
                add("ERROR", "제목에 < > 사용 불가")
        d = r["new_description"]
        if d:
            size = len(d.encode("utf-8"))
            if size > DESCRIPTION_MAX_BYTES:
                add("ERROR", f"설명이 {size}바이트 (최대 {DESCRIPTION_MAX_BYTES}바이트, 한글은 1자=3바이트)")
            if "<" in d or ">" in d:
                add("ERROR", "설명에 < > 사용 불가")
            n = len(HASHTAG_RE.findall(d))
            if n > HASHTAG_HARD_MAX:
                add("ERROR", f"해시태그 {n}개: {HASHTAG_HARD_MAX}개를 넘으면 유튜브가 해시태그를 전부 무시함")
            elif n > HASHTAG_RECOMMENDED_MAX:
                add("WARN", f"해시태그 {n}개: 너무 많음 (3~5개 권장)")
        if r["new_tags"]:
            n = tags_length(split_tags(r["new_tags"]))
            if n > TAGS_MAX_CHARS:
                add("ERROR", f"태그 합계 {n}자 (최대 {TAGS_MAX_CHARS}자)")
        p = r["publish_at"]
        if p and p != r["current_publish_at"]:
            try:
                dt = parse_local(p, tz)
            except ValueError as e:
                add("ERROR", str(e))
            else:
                if r["current_privacy"] != "private":
                    add("ERROR", f"'{r['current_privacy']}' 상태 영상은 예약 불가 (비공개 영상만 예약 가능)")
                if dt < now + MIN_LEAD:
                    add("ERROR", f"예약 시간 {p} 이 이미 지났거나 너무 가까움")
        if p:
            try:
                dt = parse_local(p, tz)
                if dt in seen_times:
                    add("WARN", f"예약 시간 {p} 이 '{seen_times[dt]}' 과 겹침")
                else:
                    seen_times[dt] = r["new_title"] or r["current_title"]
            except ValueError:
                pass
        if r["is_short"] == "Y" and r["duration_sec"] == "0":
            add("WARN", "영상 길이가 0초로 나옴 (아직 처리 중일 수 있음)")
    return problems


def print_problems(problems) -> None:
    for r, level, msg in problems:
        mark = "✗" if level == "ERROR" else "△"
        print(f"  {mark} [{level}] {r['new_title'] or r['current_title']} ({r['video_id']}): {msg}")


def cmd_check(cfg, args):
    tz = tz_of(cfg)
    rows = load_plan(resolve(cfg, "plan_file"))
    problems = check_rows(rows, tz, datetime.now(tz))
    if not problems:
        print(f"문제 없음 ({len(rows)}개 검사)")
        return
    print_problems(problems)
    errors = sum(1 for p in problems if p[1] == "ERROR")
    print(f"오류 {errors}개, 주의 {len(problems) - errors}개")
    if errors:
        sys.exit(1)


# ---------------------------------------------------------------- 명령: apply / verify


def desired_changes(row: dict, video: dict, tz: ZoneInfo) -> tuple[dict | None, list[str]]:
    """plan 행과 유튜브 현재 상태를 비교해 (update 요청 body 또는 None, 변경 설명 목록)."""
    sn, st = video.get("snippet", {}), video.get("status", {})
    notes = []
    new_snip = None

    title = row["new_title"] or sn.get("title", "")
    desc = row["new_description"] if row["new_description"] else sn.get("description", "")
    tags = split_tags(row["new_tags"]) if row["new_tags"] else (sn.get("tags") or [])
    if title != sn.get("title", ""):
        notes.append(f"제목: {sn.get('title', '')!r} → {title!r}")
    if desc != sn.get("description", ""):
        preview = desc.replace("\n", " ")[:50]
        notes.append(f"설명: 변경 ({len(desc)}자) {preview}…")
    if tags != (sn.get("tags") or []):
        notes.append(f"태그: {', '.join(tags)}")
    if notes:
        new_snip = {"title": title, "description": desc, "categoryId": sn.get("categoryId", "22")}
        if tags:
            new_snip["tags"] = tags
        for k in ("defaultLanguage", "defaultAudioLanguage"):
            if sn.get(k):
                new_snip[k] = sn[k]

    new_status = None
    if row["publish_at"]:
        want = parse_local(row["publish_at"], tz)
        have = parse_api_time(st["publishAt"]) if st.get("publishAt") else None
        if have != want:
            new_status = {"privacyStatus": "private", "publishAt": to_api_time(want)}
            for k in ("selfDeclaredMadeForKids", "embeddable", "license", "publicStatsViewable", "containsSyntheticMedia"):
                if k in st:
                    new_status[k] = st[k]
            old = fmt_local(have, tz) if have else "없음"
            notes.append(f"예약: {old} → {row['publish_at']} ({tz.key})")

    if not new_snip and not new_status:
        return None, []
    body = {"id": video["id"]}
    if new_snip:
        body["snippet"] = new_snip
    if new_status:
        body["status"] = new_status
    return body, notes


def select_rows(rows, args):
    out = [r for r in rows if r["new_title"] or r["new_description"] or r["new_tags"] or r["publish_at"]]
    if args.only:
        wanted = set(split_tags(args.only))
        out = [r for r in out if r["video_id"] in wanted]
    return out


def apply_rows(yt, rows: list[dict], targets: list[dict], tz: ZoneInfo, now: datetime, execute: bool,
               limit: int | None, save, backup_dir: Path | None = None, confirm=None) -> dict:
    problems = [p for p in check_rows(targets, tz, now) if p[1] == "ERROR"]
    bad = {id(r) for r, _, _ in problems}
    if problems:
        print("다음 영상은 오류가 있어 건너뜁니다:")
        print_problems(problems)

    videos = get_videos(yt, [r["video_id"] for r in targets])
    plan = []
    for r in targets:
        if id(r) in bad:
            continue
        v = videos.get(r["video_id"])
        if not v:
            print(f"  ! 유튜브에서 찾을 수 없음: {r['current_title']} ({r['video_id']})")
            continue
        if r["publish_at"] and v.get("status", {}).get("privacyStatus") != "private":
            print(f"  ! 비공개 상태가 아니어서 예약을 건너뜀: {r['current_title']}")
            continue
        body, notes = desired_changes(r, v, tz)
        if body:
            plan.append((r, v, body, notes))
    if limit:
        plan = plan[:limit]

    stats = {"planned": len(plan), "ok": 0, "failed": 0}
    if not plan:
        print("유튜브에 반영할 변경 사항이 없습니다. (이미 모두 같음)")
        return stats

    for r, v, body, notes in plan:
        print(f"\n■ {v['snippet'].get('title', '')}  ({r['video_id']})")
        for n in notes:
            print(f"    - {n}")
    print(f"\n총 {len(plan)}개 영상 · 예상 할당량 {len(plan) * QUOTA_UPDATE} / 하루 10,000")

    if not execute:
        print("\n[미리보기] 아직 아무것도 바뀌지 않았습니다. 실제 반영: python yt.py apply --execute")
        return stats
    if confirm and not confirm(len(plan)):
        print("취소했습니다.")
        return stats

    if backup_dir:
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(tz).strftime("%Y%m%d-%H%M%S")
        bpath = backup_dir / f"before-apply-{stamp}.json"
        bpath.write_text(json.dumps({r["video_id"]: v for r, v, _, _ in plan}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"원래 값 백업: {bpath}")

    for r, v, body, notes in plan:
        parts = ",".join(k for k in ("snippet", "status") if k in body)
        try:
            yt.videos().update(part=parts, body=body).execute()
        except Exception as e:  # googleapiclient.errors.HttpError 등
            reason, msg = api_error_text(e)
            r["apply_status"] = f"실패 {datetime.now(tz):%m-%d %H:%M}: {reason or msg}"[:200]
            stats["failed"] += 1
            print(f"  ✗ {r['current_title']}: {reason} {msg}")
            save(rows)
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                print("오늘 할당량을 다 썼습니다. 내일 다시 실행하면 남은 영상만 처리합니다.")
                break
            continue
        r["apply_status"] = f"완료 {datetime.now(tz):%m-%d %H:%M}"
        if "snippet" in body:
            r["current_title"] = body["snippet"]["title"]
        if "status" in body:
            r["current_publish_at"] = r["publish_at"]
        stats["ok"] += 1
        print(f"  ✓ {body.get('snippet', {}).get('title') or r['current_title']}")
        save(rows)
    print(f"\n완료 {stats['ok']}개, 실패 {stats['failed']}개. 확인: python yt.py verify")
    return stats


def cmd_apply(cfg, args):
    tz = tz_of(cfg)
    plan_path = resolve(cfg, "plan_file")
    rows = load_plan(plan_path)
    targets = select_rows(rows, args)
    if not targets:
        print("반영할 내용이 없습니다. new_title / new_description / new_tags / publish_at 중 하나를 채워주세요.")
        return
    yt = get_service(cfg, interactive=False)

    def confirm(n):
        if args.yes:
            return True
        return input(f"\n{n}개 영상을 실제로 수정합니다. 계속할까요? (y/N) ").strip().lower() == "y"

    apply_rows(yt, rows, targets, tz, datetime.now(tz), args.execute, args.limit,
               save=lambda rs: save_plan(rs, plan_path), backup_dir=BASE / "backups", confirm=confirm)


def cmd_verify(cfg, args):
    tz = tz_of(cfg)
    rows = load_plan(resolve(cfg, "plan_file"))
    targets = select_rows(rows, args)
    yt = get_service(cfg, interactive=False)
    videos = get_videos(yt, [r["video_id"] for r in targets])
    ok = 0
    for r in targets:
        v = videos.get(r["video_id"])
        if not v:
            print(f"  ✗ 찾을 수 없음: {r['current_title']}")
            continue
        _, notes = desired_changes(r, v, tz)
        if notes:
            print(f"  ✗ 아직 다름: {r['new_title'] or r['current_title']}")
            for n in notes:
                print(f"      - {n}")
        else:
            ok += 1
    print(f"plan.csv 와 일치: {ok}/{len(targets)}")


# ---------------------------------------------------------------- 명령: linklist


def cmd_linklist(cfg, args):
    plan_path = resolve(cfg, "plan_file")
    rows = [r for r in load_plan(plan_path) if r["related_longform"]]
    if not rows:
        print("related_longform 열이 비었습니다. plan.csv 에 쇼츠별로 연결할 롱폼(제목 또는 링크)을 적어주세요.")
        return
    lines = [
        "# 관련 동영상(롱폼) 연결 체크리스트",
        "",
        "스튜디오 링크 → '관련 동영상' → 아래 롱폼 선택 → 저장. (공식 API 미지원이라 직접 설정)",
        "",
    ]
    for r in rows:
        lines.append(f"- [ ] {r['new_title'] or r['current_title']} → **{r['related_longform']}**  ")
        lines.append(f"      {r['studio_link']}")
    out = plan_path.with_name("longform_checklist.md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)}개 → {out}")


# ---------------------------------------------------------------- 명령: calendar (예약 달력)

CALENDAR_FILE = "calendar.html"


def calendar_events(videos: dict, tz: ZoneInfo, max_short: int) -> tuple[list[dict], dict]:
    """영상 목록 -> 달력 이벤트. 예약된 영상은 예약 시각, 공개된 영상은 공개 시각에 표시."""
    events, undated = [], {"long": 0, "short": 0}
    for vid, v in videos.items():
        st, sn = v.get("status", {}), v.get("snippet", {})
        dur = iso_duration_seconds(v.get("contentDetails", {}).get("duration", ""))
        kind = "short" if dur <= max_short else "long"
        privacy = st.get("privacyStatus")
        if st.get("publishAt") and privacy != "public":
            when, state = parse_api_time(st["publishAt"]), "scheduled"
        elif privacy == "public" and sn.get("publishedAt"):
            when, state = parse_api_time(sn["publishedAt"]), "published"
        else:
            if privacy == "private":
                undated[kind] += 1
            continue
        local = when.astimezone(tz)
        events.append({
            "id": vid, "title": sn.get("title", ""), "kind": kind, "state": state,
            "date": local.strftime("%Y-%m-%d"), "time": local.strftime("%H:%M"),
        })
    events.sort(key=lambda e: (e["date"], e["time"]))
    return events, undated


def render_calendar(events: list[dict], undated: dict, channel: str, now: datetime) -> str:
    template = (BASE / "calendar_template.html").read_text(encoding="utf-8")
    data = {
        "channel": channel, "today": now.strftime("%Y-%m-%d"), "generated": now.strftime("%Y-%m-%d %H:%M"),
        "events": events, "undated": undated,
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")  # 제목에 </script> 가 있어도 안전
    return template.replace("/*__DATA__*/null", payload)


def cmd_calendar(cfg, args):
    tz = tz_of(cfg)
    yt = get_service(cfg, interactive=True)
    channel_title, ids = list_upload_ids(yt)
    videos = get_videos(yt, ids)
    events, undated = calendar_events(videos, tz, int(cfg["shorts_max_seconds"]))
    out = resolve(cfg, "plan_file").with_name(CALENDAR_FILE)
    out.write_text(render_calendar(events, undated, channel_title, datetime.now(tz)), encoding="utf-8")
    scheduled = [e for e in events if e["state"] == "scheduled"]
    longs = sum(1 for e in scheduled if e["kind"] == "long")
    print(f"예약된 롱폼 {longs}개 · 쇼츠 {len(scheduled) - longs}개 → {out.name} (브라우저로 열기)")
    if sys.platform == "win32":
        try:
            os.startfile(str(out))
        except OSError:
            pass


# ---------------------------------------------------------------- 명령: export / import (Claude 대화창과 주고받기, 무료)

EXPORT_FILE = "for_claude.txt"
IMPORT_FILE = "tags.txt"


def open_in_notepad(path: Path) -> None:
    if sys.platform == "win32":
        try:
            os.startfile(str(path))  # 윈도우: 기본 프로그램(메모장)으로 열기
        except OSError:
            pass


def cmd_export(cfg, args):
    """아직 해시태그가 없는 영상의 'video_id|제목' 목록을 만들어 Claude 대화창에 붙여넣게 함."""
    plan_path = resolve(cfg, "plan_file")
    rows = [r for r in load_plan(plan_path) if args.all or not r["new_description"]]
    if not rows:
        print("새로 해시태그를 넣을 영상이 없습니다. (모두 작성됨 · 전체를 다시 하려면 --all)")
        return
    lines = [f"{r['video_id']}|{r['current_title']}" + (f"|메모: {r['memo']}" if r["memo"] else "") for r in rows]
    out = plan_path.with_name(EXPORT_FILE)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)}개 영상 목록 → {out.name}")
    print("메모장 내용을 전체 복사(Ctrl+A, Ctrl+C)해서 Claude 에게 '해시태그 써줘' 라고 붙여넣으세요.")
    open_in_notepad(out)


def parse_import_lines(text: str) -> dict[str, list[str]]:
    """'video_id|설명 본문|#해시태그들|검색 태그' 형식 줄들을 읽음 (앞뒤 공백·빈 줄 무시)."""
    out = {}
    for line in text.splitlines():
        parts = [x.strip() for x in line.strip().split("|")]
        if len(parts) == 4 and parts[0]:
            out[parts[0]] = parts
    return out


def import_rows(rows: list[dict], items: dict[str, list[str]], cache: dict, keep_existing: bool = True) -> tuple[int, int, list[str]]:
    """plan 행에 설명 · 해시태그 · 태그를 채움. 이미 설명이 있는 영상은 그 글을 살리고 해시태그만 붙임."""
    cfg = {"writing": {"max_generated_hashtags": 20, "fixed_hashtags": []}}
    by_id = {r["video_id"]: r for r in rows}
    filled = kept = 0
    for vid, (_, body, hashtags, tags) in items.items():
        r = by_id.get(vid)
        if not r:
            continue
        old = (cache.get(vid, {}).get("snippet", {}).get("description") or "").strip()
        base = old if (keep_existing and old) else body
        r["new_description"] = compose_description(base, hashtags.split(), cfg)
        r["new_tags"] = tags
        filled += 1
        kept += bool(keep_existing and old)
    missing = [vid for vid in items if vid not in by_id]
    return filled, kept, missing


def cmd_import(cfg, args):
    plan_path = resolve(cfg, "plan_file")
    src = Path(args.file) if args.file else plan_path.with_name(IMPORT_FILE)
    if not src.exists():
        src.write_text("", encoding="utf-8")
        print(f"{src.name} 를 메모장으로 엽니다. Claude 가 준 내용을 붙여넣고 저장(Ctrl+S)한 뒤 이 명령을 다시 실행하세요.")
        open_in_notepad(src)
        return
    items = parse_import_lines(src.read_text(encoding="utf-8-sig"))
    if not items:
        print(f"{src.name} 에 읽을 내용이 없습니다. Claude 가 준 내용을 붙여넣고 저장한 뒤 다시 실행하세요.")
        open_in_notepad(src)
        return
    rows = load_plan(plan_path)
    cache_path = plan_path.with_name("videos_cache.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    filled, kept, missing = import_rows(rows, items, cache, cfg["writing"].get("keep_existing_description", True))
    save_plan(rows, plan_path)
    print(f"{filled}개 영상에 해시태그·태그 채움 (그중 기존 설명 유지 {kept}개)")
    if missing:
        print(f"※ plan.csv 에 없는 영상 {len(missing)}개는 건너뜀: {', '.join(missing[:5])}")
    if filled:
        src.rename(src.with_name(f"tags_done_{datetime.now():%Y%m%d-%H%M%S}.txt"))  # 다음번에 섞이지 않게 보관
        print("다음: python yt.py apply  (미리보기)")


# ---------------------------------------------------------------- CLI


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 윈도우 콘솔 한글 출력
    p = argparse.ArgumentParser(description="유튜브 쇼츠 예약·제목·설명 자동화 (공식 API)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth", help="구글 계정 연결")

    f = sub.add_parser("fetch", help="비공개 쇼츠 목록을 plan.csv 로 가져오기")
    f.add_argument("--all-types", action="store_true", help="쇼츠가 아닌 롱폼도 포함")
    f.add_argument("--include-public", action="store_true", help="공개/일부공개 영상도 포함 (제목·설명 수정용)")

    g = sub.add_parser("generate", help="Claude 로 제목·설명·태그 작성")
    g.add_argument("--overwrite", action="store_true", help="이미 쓴 것도 다시 작성")
    g.add_argument("--limit", type=int, help="최대 N개만")
    g.add_argument("--only", help="이 video_id 들만 (쉼표 구분)")
    g.add_argument("--include-long", action="store_true", help="롱폼도 포함")

    s = sub.add_parser("schedule", help="예약 시간 자동 배정")
    s.add_argument("--start", help="시작 날짜 YYYY-MM-DD (기본: 오늘)")
    s.add_argument("--times", help="하루 시간들, 예: 18:00,20:00,22:00 (기본: config.yaml)")
    s.add_argument("--overwrite", action="store_true", help="이미 정해진 시간도 다시 배정")
    s.add_argument("--limit", type=int, help="최대 N개만")
    s.add_argument("--include-long", action="store_true", help="롱폼도 포함")

    sub.add_parser("check", help="plan.csv 검사")

    a = sub.add_parser("apply", help="유튜브에 반영 (기본은 미리보기)")
    a.add_argument("--execute", action="store_true", help="실제로 반영")
    a.add_argument("--limit", type=int, help="최대 N개만 (처음엔 --limit 1 로 테스트 권장)")
    a.add_argument("--only", help="이 video_id 들만 (쉼표 구분)")
    a.add_argument("--yes", action="store_true", help="확인 질문 생략")

    v = sub.add_parser("verify", help="반영 결과 확인")
    v.add_argument("--only", help="이 video_id 들만 (쉼표 구분)")

    sub.add_parser("linklist", help="관련 동영상 수동 연결 체크리스트")

    sub.add_parser("calendar", help="롱폼·쇼츠 예약 달력을 브라우저로 보기")

    e = sub.add_parser("export", help="해시태그 없는 영상 목록을 Claude 에게 줄 형태로 만들기")
    e.add_argument("--all", action="store_true", help="이미 작성된 영상도 포함")

    i = sub.add_parser("import", help="Claude 가 준 해시태그(tags.txt)를 plan.csv 에 채우기")
    i.add_argument("file", nargs="?", help="읽을 파일 (기본: tags.txt)")

    args = p.parse_args(argv)
    cfg = load_config()
    handler = {
        "auth": cmd_auth, "fetch": cmd_fetch, "generate": cmd_generate, "schedule": cmd_schedule,
        "check": cmd_check, "apply": cmd_apply, "verify": cmd_verify, "linklist": cmd_linklist,
        "export": cmd_export, "import": cmd_import, "calendar": cmd_calendar,
    }[args.cmd]
    try:
        handler(cfg, args)
    except UserError as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
