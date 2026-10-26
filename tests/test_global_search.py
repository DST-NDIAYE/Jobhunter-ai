"""Recherche globale : pages HTTP simulées, CV et SQLite réels temporaires."""

from dataclasses import replace
from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
import pymupdf

from app.config import Settings
from app.database import add_job, get_jobs, init_db
from app.france_travail import FranceTravailClient, FranceTravailError, InvalidSearchError, JobPage
from app.global_search import clean_keywords, search_global


def job(external_id, title="Data Scientist", description="Python SQL"):
    return {"external_id": str(external_id), "source": "france_travail", "title": title,
            "description": description, "experience": "6 ans"}


class PaginationTests(unittest.TestCase):
    def make_client(self, responses):
        self.requests = []
        responses = iter([httpx.Response(200, json={"access_token": "simulated", "expires_in": 3600}), *responses])
        def handle(request):
            self.requests.append(request)
            return next(responses)
        transport = self.enterContext(httpx.Client(transport=httpx.MockTransport(handle)))
        return FranceTravailClient(Settings(france_travail_client_id="fake", france_travail_client_secret="fake"), transport)

    def test_multiple_pages_and_final_content_range(self):
        client = self.make_client([
            httpx.Response(206, headers={"Content-Range": "offres 0-149/152"}, json={"resultats": [{"id": str(i)} for i in range(150)]}),
            httpx.Response(200, headers={"Content-Range": "offres 150-151/152"}, json={"resultats": [{"id": "150"}, {"id": "151"}]}),
        ])
        first = client.search_page("Data", limit=150, france_only=True)
        second = client.search_page("Data", limit=150, offset=first.next_offset, france_only=True)
        self.assertEqual(first.total, 152)
        self.assertEqual(len(first.jobs) + len(second.jobs), 152)
        self.assertIsNone(second.next_offset)
        self.assertEqual(self.requests[-1].url.params["range"], "150-299")
        self.assertEqual(self.requests[-1].url.params["pays"], "99100")
        self.assertNotIn("commune", self.requests[-1].url.params)

    def test_short_page_with_total_still_continues(self):
        client = self.make_client([httpx.Response(206, headers={"Content-Range": "offres 0-0/3"}, json={"resultats": [{"id": "1"}]})])
        self.assertEqual(client.search_page(limit=20).next_offset, 1)

    def test_empty_and_range_exhausted_terminate(self):
        for response in (httpx.Response(204), httpx.Response(416), httpx.Response(200, json={"resultats": []})):
            with self.subTest(status=response.status_code):
                page = self.make_client([response]).search_page(offset=150)
                self.assertEqual(page.jobs, [])
                self.assertIsNone(page.next_offset)

    def test_missing_header_uses_bounded_fallback(self):
        client = self.make_client([httpx.Response(206, json={"resultats": [{"id": "1"}, {"id": "2"}]})])
        self.assertEqual(client.search_page(limit=2).next_offset, 2)

    def test_window_limit_and_invalid_offsets(self):
        client = self.make_client([httpx.Response(206, headers={"Content-Range": "offres 999-999/5000"}, json={"resultats": [{"id": "1"}]})])
        page = client.search_page(offset=999, limit=150)
        self.assertTrue(page.window_reached)
        self.assertIsNone(page.next_offset)
        self.assertEqual(self.requests[-1].url.params["range"], "999-999")
        for offset in (-1, 1000, True, 1.5):
            with self.assertRaises(InvalidSearchError):
                client.search_page(offset=offset)

    def test_invalid_or_nonadvancing_content_range_is_reported(self):
        for header in ("invalid", "offres 0-0/10", "offres 150-151/151"):
            with self.subTest(header=header), self.assertRaises(FranceTravailError):
                self.make_client([httpx.Response(206, headers={"Content-Range": header}, json={"resultats": [{"id": "1"}]})]).search_page(offset=150)

    def test_rate_limit_preserves_retry_after(self):
        client = self.make_client([httpx.Response(429, headers={"Retry-After": "60"})])
        with self.assertRaises(FranceTravailError) as caught:
            client.search_page()
        self.assertEqual(caught.exception.retry_after, 60)


class GlobalSearchTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Settings(database_path=Path(folder) / "jobs.db", cv_path=Path(folder) / "cv.pdf",
                                 france_travail_client_id="fake", france_travail_client_secret="fake")
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "Python SQL")
            document.save(self.settings.cv_path)
        self.enterContext(patch("app.global_search.get_settings", return_value=self.settings))
        self.sleep = self.enterContext(patch("app.global_search.sleep"))
        self.client = Mock()

    def test_cross_keyword_deduplication_global_ranking_and_storage(self):
        self.client.search_page.side_effect = [
            JobPage([job("1", description="cuisine"), job("2")]),
            JobPage([job("2"), job("3", "Senior ML Engineer")]),
        ]
        progress = Mock()
        result = search_global(["Data Scientist", "ML Engineer"], on_progress=progress, client=self.client)
        self.assertEqual((result["raw_count"], result["duplicates"], result["unique_count"]), (4, 1, 3))
        self.assertEqual(result["keywords_searched"], 2)
        self.assertEqual([offer["match_score"] for offer in result["jobs"]], [100, 100, 0])
        self.assertEqual(result["jobs"][0]["keywords"], ["Data Scientist", "ML Engineer"])
        self.assertTrue(any(offer["experience"] == "6 ans" and "Senior" in offer["title"] for offer in result["jobs"]))
        stored = get_jobs(self.settings.database_path)
        self.assertEqual(len(stored), 3)
        self.assertTrue(all(row["first_seen_at"] and row["last_seen_at"] for row in stored))
        self.assertTrue(progress.called)

    def test_round_robin_pagination_and_unique_global_limit(self):
        def page(**params):
            offset, limit, keyword = params["offset"], params["limit"], params["keyword"]
            return JobPage([job(f"{keyword}-{i}") for i in range(offset, offset + limit)], offset + limit, 10000)
        self.client.search_page.side_effect = page
        report = search_global(["Data", "ML"], limit=500, client=self.client)
        self.assertEqual(report["unique_count"], 500)
        self.assertTrue(report["limit_reached"])
        calls = self.client.search_page.call_args_list
        self.assertEqual([call.kwargs["keyword"] for call in calls], ["Data", "ML", "Data", "ML"])
        self.assertEqual([call.kwargs["offset"] for call in calls], [0, 0, 150, 150])
        self.assertTrue(all(call.kwargs["limit"] <= 150 for call in calls))

    def test_all_fourteen_keywords_receive_a_turn_with_limit_fifty(self):
        self.client.search_page.side_effect = lambda **kw: JobPage([job(f"{kw['keyword']}-{i}") for i in range(kw["limit"])])
        report = search_global([f"Métier {i}" for i in range(14)], limit=50, client=self.client)
        self.assertEqual(report["keywords_searched"], 14)
        self.assertEqual(report["unique_count"], 50)

    def test_one_keyword_failure_keeps_other_keywords_and_previous_pages(self):
        self.client.search_page.side_effect = [JobPage([job("1")], 1, 2), JobPage([job("2")]), FranceTravailError("Indisponible")]
        report = search_global(["Data", "ML"], client=self.client)
        self.assertEqual(report["unique_count"], 2)
        self.assertEqual(report["errors"], [{"keyword": "Data", "offset": 1, "message": "Indisponible"}])
        self.assertEqual(len(get_jobs(self.settings.database_path)), 2)

    def test_repeated_page_stops_without_infinite_loop(self):
        self.client.search_page.side_effect = [JobPage([job("1")], 1), JobPage([job("1")], 2)]
        report = search_global(["Data"], client=self.client)
        self.assertEqual(report["request_count"], 2)
        self.assertEqual(report["duplicates"], 1)
        self.assertTrue(report["warnings"])

    def test_request_budget_is_bounded(self):
        self.client.search_page.return_value = JobPage([job("1")], 1)
        with patch("app.global_search.MAX_REQUESTS", 1):
            report = search_global(["Data", "ML"], client=self.client)
        self.assertEqual(report["request_count"], 1)
        self.assertTrue(any("Budget" in warning for warning in report["warnings"]))

    def test_retry_after_short_wait_continues_other_keywords(self):
        self.client.search_page.side_effect = [FranceTravailError("Quota", 503, retry_after=2), JobPage([job("2")])]
        report = search_global(["Data", "ML"], client=self.client)
        self.sleep.assert_any_call(2)
        self.assertEqual(report["unique_count"], 1)
        self.assertEqual(report["keywords_searched"], 2)

    def test_retry_after_long_wait_stops_requests(self):
        self.client.search_page.side_effect = FranceTravailError("Quota", 503, retry_after=60)
        report = search_global(["Data", "ML"], client=self.client)
        self.assertEqual(report["request_count"], 1)
        self.assertTrue(any("Retry-After" in warning for warning in report["warnings"]))

    def test_repeat_search_updates_last_seen_and_keeps_first_seen_and_keywords(self):
        self.client.search_page.return_value = JobPage([job("1")])
        search_global(["Data"], client=self.client)
        original = get_jobs(self.settings.database_path)[0]
        with closing(sqlite3.connect(self.settings.database_path)) as connection, connection:
            connection.execute("UPDATE jobs SET last_seen_at = '2000-01-01T00:00:00Z'")
        search_global(["ML"], client=self.client)
        stored = get_jobs(self.settings.database_path)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["first_seen_at"], original["first_seen_at"])
        self.assertNotEqual(stored[0]["last_seen_at"], "2000-01-01T00:00:00Z")
        self.assertEqual(stored[0]["keywords"], ["Data", "ML"])

    def test_migrate_existing_v1_database_without_losing_data(self):
        init_db(self.settings.database_path)
        add_job(job("legacy"), self.settings.database_path)
        with closing(sqlite3.connect(self.settings.database_path)) as connection, connection:
            for column in ("first_seen_at", "last_seen_at", "keywords"):
                connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        init_db(self.settings.database_path)
        init_db(self.settings.database_path)
        old = get_jobs(self.settings.database_path)[0]
        self.assertEqual(old["first_seen_at"], old["created_at"])
        self.assertEqual(old["last_seen_at"], old["created_at"])
        self.assertEqual(old["external_id"], "legacy")

    def test_keyword_validation_and_case_deduplication(self):
        self.assertEqual(clean_keywords([" Data  Scientist ", "data scientist", "ML", ""]), ["Data Scientist", "ML"])
        for keywords in ([], [" "], ["x" * 201], [str(i) for i in range(51)]):
            with self.assertRaises(ValueError):
                search_global(keywords, client=self.client)
        for limit in (0, 501, True, 1.5):
            with self.assertRaises(ValueError):
                search_global(["Data"], limit=limit, client=self.client)
        self.client.search_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
