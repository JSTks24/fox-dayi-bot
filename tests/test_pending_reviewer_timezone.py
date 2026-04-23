import datetime
import unittest

from cogs import pending_reviewer as pending_questions_reviewer


class PendingReviewerTimezoneTests(unittest.TestCase):
    def _make_cog(self):
        cog = object.__new__(pending_questions_reviewer.UnansweredFilter)
        cog._ai_request_logs = []
        cog._last_ai_response_note = "尚未调用 AI"
        cog._ai_expected_total_threads = 0
        cog._ai_expected_total_batches = 0
        cog._ai_batch_size = 4
        cog._ai_batch_interval_seconds = 10
        return cog

    def test_last_ai_response_txt_uses_asia_shanghai_generation_time(self):
        cog = self._make_cog()
        now = datetime.datetime(2026, 4, 20, 16, 5, 6, tzinfo=datetime.timezone.utc)

        content = cog._build_last_ai_response_txt(now=now)

        self.assertIn(
            "生成时间(Asia/Shanghai): 2026-04-21 00:05:06 +0800",
            content,
        )
        self.assertIn("(本次没有 AI 批次请求记录)", content)

    def test_ai_report_filename_uses_asia_shanghai_timestamp(self):
        cog = self._make_cog()
        now = datetime.datetime(2026, 4, 20, 16, 5, 6, tzinfo=datetime.timezone.utc)

        filename = cog._build_ai_report_filename(now=now)

        self.assertEqual(
            filename,
            "unanswered_last_ai_response_20260421_000506_Asia-Shanghai.txt",
        )

    def test_daily_report_title_uses_asia_shanghai_date(self):
        cog = self._make_cog()
        now = datetime.datetime(2026, 4, 20, 18, 0, 0, tzinfo=datetime.timezone.utc)

        title = cog._build_daily_report_title(now=now)

        self.assertEqual(title, "📅 2026-04-21 待解决问题汇总（Asia/Shanghai）")


if __name__ == "__main__":
    unittest.main()
