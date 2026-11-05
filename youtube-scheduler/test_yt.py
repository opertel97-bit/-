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

    def test_description(self):
        d = yt.compose_description("본문입니다.", ["고양이 밈", "#shorts", "냥", "초과"], self.cfg)
        self.assertEqual(d, "본문입니다.\n\n전체 영상은 채널에서!\n\n#shorts #고양이밈")

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


class ClaudeTests(unittest.TestCase):
    def test_call_claude_parses_structured_output(self):
        captured = {}

        def create(**kw):
            captured.update(kw)
            text = json.dumps({"items": [{"video_id": "a", "title": "T", "description": "D", "hashtags": ["#x"], "tags": ["y"]}]})
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)])

        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        cfg = {"channel": {"name": "채널"}, "writing": {}, "claude": {}}
        out = yt.call_claude(client, cfg, [{"video_id": "a"}])
        self.assertEqual(out[0]["title"], "T")
        self.assertEqual(captured["model"], "claude-opus-5-5")
        self.assertEqual(captured["output_config"]["format"]["type"], "json_schema")

    def test_refusal_raises(self):
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[]))))
        with self.assertRaises(yt.UserError):
            yt.call_claude(client, {"channel": {}, "writing": {}, "claude": {}}, [])


if __name__ == "__main__":
    unittest.main()
