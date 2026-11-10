"""오프라인 테스트 (유튜브/Claude 에 실제로 접속하지 않음).  실행: python -m unittest test_yt"""
import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yt

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=KST)


def row(vid, **kw):
    r = {c: "" for c in yt.COLUMNS}
    r.update(video_id=vid, current_title=f"쇼츠 {vid}", current_privacy="private", is_short="Y", duration_sec="40")
    r.update(kw)
    return r


def video(vid, privacy="private", publish_at=None, title=None, tags=None):
    st = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True, "uploadStatus": "processed"}
    if publish_at:
        st["publishAt"] = publish_at
    sn = {"title": title or f"쇼츠 {vid}", "description": "", "categoryId": "24", "defaultLanguage": "ko"}
    if tags:
        sn["tags"] = tags
    return {"id": vid, "snippet": sn, "status": st}


class FakeYouTube:
    """videos().list / videos().update 만 흉내내는 가짜 API."""

    def __init__(self, videos, fail=None):
        self.store = {v["id"]: v for v in videos}
        self.updates = []
        self.fail = fail or {}

    def videos(self):
        return self

    def list(self, part, id, maxResults=50):
        items = [json.loads(json.dumps(self.store[i])) for i in id.split(",") if i in self.store]
        return SimpleNamespace(execute=lambda: {"items": items})

    def update(self, part, body):
        def run():
            if body["id"] in self.fail:
                err = Exception("boom")
                err.content = json.dumps({"error": {"message": "nope", "errors": [{"reason": self.fail[body["id"]]}]}}).encode()
                raise err
            self.updates.append((part, body))
            v = self.store[body["id"]]
            for k in ("snippet", "status"):
                if k in body:
                    v[k].update(body[k])
            return v
        return SimpleNamespace(execute=run)


class TimeTests(unittest.TestCase):
    def test_parse_and_convert(self):
        dt = yt.parse_local("2026/11/10 18:00", KST)
        self.assertEqual(yt.to_api_time(dt), "2026-11-10T09:00:00Z")
        self.assertEqual(yt.fmt_local(yt.parse_api_time("2026-11-10T09:00:00.000Z"), KST), "2026-11-10 18:00")

    def test_duration(self):
        self.assertEqual(yt.iso_duration_seconds("PT1M5S"), 65)
        self.assertEqual(yt.iso_duration_seconds("PT2H"), 7200)
        self.assertEqual(yt.iso_duration_seconds(""), 0)


class ScheduleTests(unittest.TestCase):
    def test_fills_empty_slots_in_order_and_skips_taken(self):
        rows = [row("a"), row("b", publish_at="2026-11-10 20:00"), row("c"), row("d", current_privacy="public"), row("e")]
        times = [time(18), time(20), time(22)]
        changes = yt.plan_schedule(rows, date(2026, 11, 10), times, KST, NOW)
        got = {r["video_id"]: r["publish_at"] for r in rows}
        self.assertEqual(got["a"], "2026-11-10 18:00")
        self.assertEqual(got["b"], "2026-11-10 20:00")  # 이미 있던 시간 유지
        self.assertEqual(got["c"], "2026-11-10 22:00")  # 20:00 은 b 가 사용 중
        self.assertEqual(got["d"], "")  # 공개 영상은 예약 대상 아님
        self.assertEqual(got["e"], "2026-11-11 18:00")
        self.assertEqual(len(changes), 3)

    def test_skips_past_slots(self):
        rows = [row("a")]
        yt.plan_schedule(rows, date(2026, 11, 5), [time(9), time(12), time(18)], KST, NOW)
        self.assertEqual(rows[0]["publish_at"], "2026-11-05 18:00")

    def test_overwrite_reassigns(self):
        rows = [row("a", publish_at="2026-12-01 18:00"), row("b", publish_at="2026-12-01 20:00")]
        yt.plan_schedule(rows, date(2026, 11, 10), [time(19)], KST, NOW, overwrite=True)
        self.assertEqual([r["publish_at"] for r in rows], ["2026-11-10 19:00", "2026-11-11 19:00"])


class CheckTests(unittest.TestCase):
    def levels(self, r):
        return [(lvl, msg) for _, lvl, msg in yt.check_rows([r], KST, NOW)]

    def test_errors(self):
        self.assertTrue(any("제목이" in m for _, m in self.levels(row("a", new_title="가" * 101))))
        self.assertTrue(any("< >" in m for _, m in self.levels(row("a", new_title="<특가>"))))
        self.assertTrue(any("지났" in m for _, m in self.levels(row("a", publish_at="2026-11-01 18:00"))))
        self.assertTrue(any("비공개" in m for _, m in self.levels(row("a", current_privacy="unlisted", publish_at="2026-12-01 18:00"))))
        self.assertTrue(any("60개" in m for _, m in self.levels(row("a", new_description=" ".join(f"#t{i}" for i in range(61))))))

    def test_duplicate_time_warns(self):
        rows = [row("a", publish_at="2026-12-01 18:00"), row("b", publish_at="2026-12-01 18:00")]
        probs = yt.check_rows(rows, KST, NOW)
        self.assertEqual([p[1] for p in probs], ["WARN"])

    def test_clean_row(self):
        self.assertEqual(self.levels(row("a", new_title="좋은 제목", new_description="본문\n\n#shorts", publish_at="2026-12-01 18:00")), [])


class ComposeTests(unittest.TestCase):
    cfg = {"writing": {"fixed_hashtags": ["#shorts"], "max_generated_hashtags": 2, "description_footer": "전체 영상은 채널에서!",
                       "default_tags": ["채널"], "max_generated_tags": 3}}

    def test_description_topic_hashtags_first(self):
        d = yt.compose_description("본문입니다.", ["췌장암 증상", "#shorts", "초과"], self.cfg)
        self.assertEqual(d, "본문입니다.\n\n전체 영상은 채널에서!\n\n#췌장암증상 #shorts")

    def test_existing_body_not_duplicated(self):
        body = "기존 설명\n\n전체 영상은 채널에서!\n#췌장암"
        d = yt.compose_description(body, ["췌장암", "소화불량"], self.cfg)
        self.assertEqual(d, body + "\n\n#소화불량 #shorts")

    def test_tags(self):
        self.assertEqual(yt.compose_tags(["고양이", "#채널", "밈", "버려짐"], self.cfg), "채널, 고양이, 밈")


class ApplyTests(unittest.TestCase):
    def run_apply(self, rows, fake, execute=True, limit=None):
        saved = []
        stats = yt.apply_rows(fake, rows, yt.select_rows(rows, SimpleNamespace(only=None)), KST, NOW, execute, limit,
                              save=lambda rs: saved.append(True), confirm=lambda n: True)
        return stats, saved

    def test_dry_run_changes_nothing(self):
        fake = FakeYouTube([video("a")])
        stats, _ = self.run_apply([row("a", new_title="새 제목", publish_at="2026-11-10 18:00")], fake, execute=False)
        self.assertEqual(stats["planned"], 1)
        self.assertEqual(fake.updates, [])

    def test_schedule_and_metadata(self):
        fake = FakeYouTube([video("a")])
        r = row("a", new_title="새 제목", new_description="설명\n\n#shorts", new_tags="고양이, 밈", publish_at="2026-11-10 18:00")
        stats, saved = self.run_apply([r], fake)
        self.assertEqual(stats["ok"], 1)
        part, body = fake.updates[0]
        self.assertEqual(part, "snippet,status")
        self.assertEqual(body["snippet"], {"title": "새 제목", "description": "설명\n\n#shorts", "categoryId": "24",
                                           "tags": ["고양이", "밈"], "defaultLanguage": "ko"})
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["publishAt"], "2026-11-10T09:00:00Z")
        self.assertFalse(body["status"]["selfDeclaredMadeForKids"])
        self.assertTrue(r["apply_status"].startswith("완료"))
        # 다시 실행하면 이미 같으므로 아무것도 안 보냄 (중복 방지)
        stats2, _ = self.run_apply([r], fake)
        self.assertEqual(stats2["planned"], 0)
        self.assertEqual(len(fake.updates), 1)

    def test_schedule_only_sends_status_part(self):
        fake = FakeYouTube([video("a")])
        self.run_apply([row("a", publish_at="2026-11-10 18:00")], fake)
        self.assertEqual(fake.updates[0][0], "status")

    def test_never_schedules_public_video(self):
        fake = FakeYouTube([video("a", privacy="public")])
        stats, _ = self.run_apply([row("a", publish_at="2026-11-10 18:00")], fake)  # plan 상 private 이어도 실제 상태로 재확인
        self.assertEqual(fake.updates, [])
        self.assertEqual(stats["planned"], 0)

    def test_limit_and_failure_recorded(self):
        fake = FakeYouTube([video("a"), video("b"), video("c")], fail={"a": "forbidden"})
        rows = [row(v, new_title=f"새 {v}") for v in "abc"]
        stats, _ = self.run_apply(rows, fake, limit=2)
        self.assertEqual((stats["ok"], stats["failed"]), (1, 1))
        self.assertIn("forbidden", rows[0]["apply_status"])
        self.assertEqual(rows[2]["apply_status"], "")  # limit 밖은 손대지 않음

    def test_quota_stops_loop(self):
        fake = FakeYouTube([video("a"), video("b")], fail={"a": "quotaExceeded"})
        stats, _ = self.run_apply([row("a", new_title="x"), row("b", new_title="y")], fake)
        self.assertEqual(fake.updates, [])
        self.assertEqual(stats["failed"], 1)


class CsvTests(unittest.TestCase):
    def test_roundtrip_keeps_korean_and_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.csv"
            rows = [row("a", new_title="제목, 쉼표", new_description="첫 줄\n\n#쇼츠")]
            yt.save_plan(rows, p)
            self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))  # 엑셀용 BOM
            self.assertEqual(yt.load_plan(p)[0]["new_description"], "첫 줄\n\n#쇼츠")
            self.assertEqual(yt.load_plan(p)[0]["new_title"], "제목, 쉼표")


class FakeClaude:
    def __init__(self, items):
        self.items, self.calls = items, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        text = json.dumps({"items": self.items})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)])


BASE_CFG = {"channel": {"name": "운동과 건강함"}, "writing": {"fixed_hashtags": ["#shorts"]}, "claude": {}}


class ClaudeTests(unittest.TestCase):
    def test_schema_has_no_title_by_default(self):
        props = yt.gen_schema(BASE_CFG)["properties"]["items"]["items"]["properties"]
        self.assertNotIn("title", props)
        cfg = {**BASE_CFG, "writing": {"generate_title": True}}
        self.assertIn("title", yt.gen_schema(cfg)["properties"]["items"]["items"]["properties"])
        self.assertNotIn("새 제목", yt.build_system_prompt(BASE_CFG))

    def test_call_claude_parses_structured_output(self):
        fake = FakeClaude([{"video_id": "a", "target_viewer": "50대", "description": "D", "hashtags": ["#x"], "tags": ["y"]}])
        out = yt.call_claude(fake, BASE_CFG, [{"video_id": "a"}])
        self.assertEqual(out[0]["description"], "D")
        self.assertEqual(fake.calls[0]["model"], "claude-opus-5-5")
        self.assertEqual(fake.calls[0]["output_config"]["format"]["type"], "json_schema")

    def test_refusal_raises(self):
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[]))))
        with self.assertRaises(yt.UserError):
            yt.call_claude(client, BASE_CFG, [])

    def test_generate_keeps_titles_and_existing_description(self):
        import anthropic
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.csv"
            yt.save_plan([row("a", current_title="췌장암, 이 증상 놓치면 늦습니다"), row("b")], plan)
            (Path(d) / "videos_cache.json").write_text(json.dumps({"b": {"snippet": {"description": "직접 쓴 설명"}}}), encoding="utf-8")
            fake = FakeClaude([
                {"video_id": "a", "target_viewer": "소화불량 50대", "description": "새 본문", "hashtags": ["췌장암", "소화불량", "건강정보"], "tags": ["췌장암 초기증상"]},
                {"video_id": "b", "target_viewer": "x", "description": "버려질 본문", "hashtags": ["무릎운동"], "tags": ["무릎"]},
            ])
            cfg = {**BASE_CFG, "plan_file": str(plan)}
            orig = anthropic.Anthropic
            anthropic.Anthropic = lambda: fake
            try:
                yt.cmd_generate(cfg, SimpleNamespace(include_long=False, overwrite=False, only=None, limit=None))
            finally:
                anthropic.Anthropic = orig
            a, b = yt.load_plan(plan)
            self.assertEqual(a["new_title"], "")  # 제목은 건드리지 않음
            self.assertEqual(a["new_description"], "새 본문\n\n#췌장암 #소화불량 #건강정보 #shorts")
            self.assertEqual(a["new_tags"], "췌장암 초기증상")
            self.assertEqual(b["new_description"], "직접 쓴 설명\n\n#무릎운동 #shorts")
            self.assertIn("췌장암, 이 증상", fake.calls[0]["messages"][0]["content"])


class ImportExportTests(unittest.TestCase):
    TEXT = """
  a|새 본문입니다.|#췌장암 #췌장암증상 #shorts|췌장암, 췌장암 증상
b|버려질 본문|#남성호르몬 #남성건강 #shorts|남성호르몬
zzz|plan 에 없음|#x|y
깨진 줄
"""

    def test_parse_and_merge_keeps_existing_description(self):
        items = yt.parse_import_lines(self.TEXT)
        self.assertEqual(set(items), {"a", "b", "zzz"})
        rows = [row("a"), row("b")]
        cache = {"b": {"snippet": {"description": "기존 설명\n#남성건강"}}}
        filled, kept, missing = yt.import_rows(rows, items, cache)
        self.assertEqual((filled, kept, missing), (2, 1, ["zzz"]))
        self.assertEqual(rows[0]["new_description"], "새 본문입니다.\n\n#췌장암 #췌장암증상 #shorts")
        self.assertEqual(rows[0]["new_tags"], "췌장암, 췌장암 증상")
        self.assertEqual(rows[1]["new_description"], "기존 설명\n#남성건강\n\n#남성호르몬 #shorts")
        # 두 번 실행해도 같은 결과
        yt.import_rows(rows, items, cache)
        self.assertEqual(rows[1]["new_description"], "기존 설명\n#남성건강\n\n#남성호르몬 #shorts")

    def test_cli_export_then_import(self):
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.csv"
            yt.save_plan([row("a", current_title="제목A"), row("b", current_title="제목B", new_description="이미 씀")], plan)
            cfg = {"plan_file": str(plan), "writing": {}}
            yt.cmd_export(cfg, SimpleNamespace(all=False))
            self.assertEqual((Path(d) / "for_claude.txt").read_text(encoding="utf-8"), "a|제목A\n")
            (Path(d) / "tags.txt").write_text("a|본문|#태그 #shorts|태그", encoding="utf-8")
            yt.cmd_import(cfg, SimpleNamespace(file=None))
            a, b = yt.load_plan(plan)
            self.assertEqual(a["new_description"], "본문\n\n#태그 #shorts")
            self.assertEqual(b["new_description"], "이미 씀")
            self.assertFalse((Path(d) / "tags.txt").exists())  # 처리 후 보관용 이름으로 바뀜


class CalendarTests(unittest.TestCase):
    def vids(self):
        def v(vid, title, dur, privacy, publish_at=None, published=None):
            st = {"privacyStatus": privacy}
            if publish_at:
                st["publishAt"] = publish_at
            return {"id": vid, "snippet": {"title": title, "publishedAt": published or "2026-11-01T00:00:00Z"},
                    "status": st, "contentDetails": {"duration": dur}}
        return {
            "L1": v("L1", "롱폼 예약", "PT25M", "private", "2026-11-12T11:00:00Z"),
            "S1": v("S1", "쇼츠 예약 </script>", "PT40S", "private", "2026-11-11T12:00:00Z"),
            "P1": v("P1", "공개된 쇼츠", "PT30S", "public", published="2026-11-09T22:00:00Z"),
            "D1": v("D1", "초안 롱폼", "PT10M", "private"),
            "D2": v("D2", "초안 쇼츠", "PT20S", "private"),
            "U1": v("U1", "일부공개", "PT20S", "unlisted"),
        }

    def test_events(self):
        events, undated = yt.calendar_events(self.vids(), KST, 180)
        got = [(e["id"], e["kind"], e["state"], e["date"], e["time"]) for e in events]
        self.assertEqual(got, [
            ("P1", "short", "published", "2026-11-10", "07:00"),
            ("S1", "short", "scheduled", "2026-11-11", "21:00"),
            ("L1", "long", "scheduled", "2026-11-12", "20:00"),
        ])
        self.assertEqual(undated, {"long": 1, "short": 1})

    def test_render_escapes_script(self):
        events, undated = yt.calendar_events(self.vids(), KST, 180)
        html = yt.render_calendar(events, undated, "운동과 건강함", NOW)
        self.assertNotIn("/*__DATA__*/null", html)
        self.assertEqual(html.count("</script>"), 1)  # 제목 속 </script> 는 이스케이프됨
        self.assertIn("운동과 건강함", html)


class UploadListTests(unittest.TestCase):
    class Fake:
        def __init__(self, playlist_pages, search_pages, search_fail=False):
            self.pl, self.sp, self.fail, self.search_calls = playlist_pages, search_pages, search_fail, 0

        def channels(self):
            return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {"items": [
                {"snippet": {"title": "운동과 건강함"}, "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}))

        def playlistItems(self):
            def lst(pageToken=None, **kw):
                i = int(pageToken or 0)
                resp = {"items": [{"contentDetails": {"videoId": v}} for v in self.pl[i]]}
                if i + 1 < len(self.pl):
                    resp["nextPageToken"] = str(i + 1)
                return SimpleNamespace(execute=lambda: resp)
            return SimpleNamespace(list=lst)

        def search(self):
            def lst(pageToken=None, **kw):
                self.search_calls += 1
                if self.fail:
                    def boom():
                        e = Exception("x")
                        e.content = b'{"error":{"message":"quota","errors":[{"reason":"quotaExceeded"}]}}'
                        raise e
                    return SimpleNamespace(execute=boom)
                i = int(pageToken or 0)
                resp = {"items": [{"id": {"videoId": v}} for v in self.sp[i]]}
                if i + 1 < len(self.sp):
                    resp["nextPageToken"] = str(i + 1)
                return SimpleNamespace(execute=lambda: resp)
            return SimpleNamespace(list=lst)

    def test_dedupes_and_adds_missing_recent_videos(self):
        fake = self.Fake([["a", "b", "a"], ["c", "b"]], [["NEW1", "a", "NEW2"], ["b", "c"], ["NEVER"]])
        title, ids = yt.list_upload_ids(fake)
        self.assertEqual(title, "운동과 건강함")
        self.assertEqual(ids, ["a", "b", "c", "NEW1", "NEW2", "NEVER"])
        self.assertEqual(fake.search_calls, 3)  # 누락이 흩어져 있으므로 검색은 끝까지

    def test_search_failure_keeps_upload_list(self):
        fake = self.Fake([["a", "a", "b"]], [], search_fail=True)
        _, ids = yt.list_upload_ids(fake)
        self.assertEqual(ids, ["a", "b"])


class RobustListingTests(unittest.TestCase):
    def test_all_my_videos_requeries_previously_seen_ids(self):
        with tempfile.TemporaryDirectory() as d:
            plan = Path(d) / "plan.csv"
            yt.save_plan([row("OLD_SHORT")], plan)
            (Path(d) / "videos_cache.json").write_text(json.dumps({"OLD_LONG": {}, "GONE": {}}), encoding="utf-8")
            asked = []
            orig_list, orig_get = yt.list_upload_ids, yt.get_videos
            yt.list_upload_ids = lambda y: ("ch", ["a"])
            def fake_get(y, ids):
                asked.extend(ids)
                return {i: {"id": i} for i in ids if i != "GONE"}
            yt.get_videos = fake_get
            try:
                title, videos = yt.all_my_videos(None, {"plan_file": str(plan)})
            finally:
                yt.list_upload_ids, yt.get_videos = orig_list, orig_get
            self.assertEqual(asked, ["a", "OLD_SHORT", "OLD_LONG", "GONE"])
            self.assertEqual(set(videos), {"a", "OLD_SHORT", "OLD_LONG"})

    def test_premiere_counts_as_scheduled(self):
        v = {"P": {"snippet": {"title": "프리미어 롱폼", "liveBroadcastContent": "upcoming", "publishedAt": "2026-11-09T00:00:00Z"},
                   "status": {"privacyStatus": "public"}, "contentDetails": {"duration": "PT30M"},
                   "liveStreamingDetails": {"scheduledStartTime": "2026-11-30T10:00:00Z"}}}
        events, _ = yt.calendar_events(v, KST, 180)
        self.assertEqual([(e["state"], e["date"], e["time"], e["kind"]) for e in events], [("scheduled", "2026-11-30", "19:00", "long")])


class CommentTests(unittest.TestCase):
    class Fake:
        def __init__(self, videos, comments):
            self.v, self.c, self.posted = videos, comments, []

        def channels(self):
            return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: {"items": [{"id": "MYCH"}]}))

        def videos(self):
            return SimpleNamespace(list=lambda part, id: SimpleNamespace(execute=lambda: {
                "items": [dict(self.v[i], id=i) for i in id.split(",") if i in self.v]}))

        def commentThreads(self):
            def lst(videoId, **kw):
                items = [{"snippet": {"topLevelComment": {"snippet": {"authorChannelId": {"value": a}}}}} for a in self.c.get(videoId, [])]
                return SimpleNamespace(execute=lambda: {"items": items})

            def ins(part, body):
                return SimpleNamespace(execute=lambda: self.posted.append(body["snippet"]))
            return SimpleNamespace(list=lst, insert=ins)

    def test_comments_only_new_public_videos_once(self):
        pub = {"status": {"privacyStatus": "public"}}
        rows = [
            row("A", publish_at="2026-11-05 08:00"),             # 공개, 댓글 없음 -> 기본 문구
            row("B", publish_at="2026-11-05 11:00"),             # 공개, 이미 채널 댓글 -> 건너뜀
            row("C", publish_at="2026-11-05 09:00", comment="풀영상은 11/30 공개 예정이에요!"),  # 직접 쓴 문구
            row("D", publish_at="2026-11-05 10:00", comment="-"),  # 댓글 안 담
            row("E", publish_at="2026-11-05 11:30"),             # 시간은 지났지만 아직 비공개 -> 다음에
            row("F", publish_at="2026-11-06 08:00"),             # 아직 미래
            row("G", publish_at="2026-10-20 08:00"),             # 너무 오래됨
        ]
        fake = self.Fake({"A": pub, "B": pub, "C": pub, "D": pub, "E": {"status": {"privacyStatus": "private"}}},
                         {"B": ["someone", "MYCH"], "A": ["viewer"]})
        texts = ["문구1", "문구2"]
        stats = yt.run_comments(fake, rows, KST, NOW, 3, texts, execute=False)
        self.assertEqual(fake.posted, [])  # 미리보기에선 안 담
        stats = yt.run_comments(fake, rows, KST, NOW, 3, texts, execute=True)
        got = {(p["videoId"], p["topLevelComment"]["snippet"]["textOriginal"]) for p in fake.posted}
        self.assertEqual({v for v, _ in got}, {"A", "C"})
        self.assertIn(("C", "풀영상은 11/30 공개 예정이에요!"), got)
        self.assertIn(dict(got)["A"], texts)
        self.assertEqual((stats["posted"], stats["done"], stats["waiting"]), (2, 1, 1))

    def test_scope_check(self):
        self.assertTrue(yt.has_scopes([yt.SCOPE_MANAGE], need_comments=False))
        self.assertFalse(yt.has_scopes([yt.SCOPE_MANAGE], need_comments=True))
        self.assertTrue(yt.has_scopes(yt.ALL_SCOPES, need_comments=True))


if __name__ == "__main__":
    unittest.main()
