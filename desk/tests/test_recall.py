import tempfile
import unittest
from datetime import datetime, timedelta

from ray import cmdlog, recall


class RecallTest(unittest.TestCase):
    def test_wants(self):
        self.assertTrue(recall.wants("先ほどの移動するかどうかの話だけど"))
        self.assertTrue(recall.wants("さっきの続き"))
        self.assertFalse(recall.wants("今日の予定は？"))

    def test_recent_reads_saved_talk_within_a_day(self):
        now = datetime(2026, 11, 28, 10, 0, tzinfo=cmdlog.JST)
        with tempfile.TemporaryDirectory() as d:
            cfg = {"cmd_dir": d}
            cmdlog.write_qa(d, "古い", "a", now=now - timedelta(days=2))
            cmdlog.write_qa(d, "知らせ", "b", now=now, extra={"brain": "n8n"})
            cmdlog.write_qa(d, "二つ目", "c", now=now - timedelta(minutes=1))
            cmdlog.write_qa(d, "一つ目", "d", now=now - timedelta(hours=2))
            self.assertEqual(recall.recent(cfg, now), [("一つ目", "d"), ("二つ目", "c")])
