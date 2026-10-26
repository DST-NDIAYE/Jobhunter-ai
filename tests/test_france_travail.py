"""Tests du protocole HTTP avec réponses simulées, sans clés ni réseau."""

from dataclasses import replace
from urllib.parse import parse_qs
import unittest
from unittest.mock import patch

import httpx

from app.config import Settings
from app.france_travail import (
    FranceTravailClient,
    FranceTravailConfigurationError,
    FranceTravailError,
    InvalidSearchError,
    normalize_job,
)


class FranceTravailTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            france_travail_client_id="test-client", france_travail_client_secret="test-secret"
        )
        self.requests = []

    def make_client(self, responses, settings=None):
        pending = iter(responses)

        def handle(request):
            self.requests.append(request)
            response = next(pending)
            if isinstance(response, Exception):
                raise response
            return response

        http_client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http_client.close)
        return FranceTravailClient(settings or self.settings, client=http_client)

    @staticmethod
    def token(token="test-token", expires_in=3600):
        return httpx.Response(200, json={"access_token": token, "expires_in": expires_in})

    def test_search_authentication_parameters_and_normalization(self):
        offer = {
            "id": "123ABCD", "intitule": " Data Scientist ",
            "entreprise": {"nom": "Exemple"}, "lieuTravail": {"libelle": "75 - PARIS"},
            "typeContrat": "CDI", "description": "Python et statistiques",
            "dateCreation": "2026-09-01T12:00:00Z",
            "salaire": {"libelle": "Annuel de 40000 Euros"},
            "experienceLibelle": "Débutant accepté",
            "competences": [{"code": "123", "libelle": "Python"}],
            "origineOffre": {"urlOrigine": "https://example.org/offre/123ABCD"},
        }
        client = self.make_client([
            httpx.Response(200, json=[{"nom": "Paris", "code": "75056"}]),
            self.token(), httpx.Response(206, json={"resultats": [offer]}),
        ])
        jobs = client.search_jobs("Data Scientist", "Paris", "cdi", 20)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["external_id"], "123ABCD")
        self.assertEqual(jobs[0]["title"], "Data Scientist")
        self.assertEqual(jobs[0]["salary"], offer["salaire"])
        self.assertEqual(jobs[0]["skills"], offer["competences"])
        self.assertEqual(jobs[0]["experience"], "Débutant accepté")
        self.assertEqual(jobs[0]["source"], "france_travail")
        geo, token, search = self.requests
        self.assertEqual(geo.url.host, "geo.api.gouv.fr")
        self.assertEqual(dict(geo.url.params), {"nom": "Paris", "fields": "nom,code"})
        self.assertNotIn("authorization", geo.headers)
        self.assertEqual(token.url.params["realm"], "/partenaire")
        self.assertEqual(token.method, "POST")
        self.assertEqual(parse_qs(token.content.decode()), {
            "grant_type": ["client_credentials"], "client_id": ["test-client"],
            "client_secret": ["test-secret"], "scope": ["api_offresdemploiv2 o2dsoffre"],
        })
        self.assertEqual(dict(search.url.params), {
            "motsCles": "Data Scientist", "commune": "75056", "distance": "0",
            "typeContrat": "CDI", "range": "0-19",
        })
        self.assertEqual(search.headers["Authorization"], "Bearer test-token")

    def test_optional_values_are_not_invented(self):
        job = normalize_job({"id": "123ABCD"})
        self.assertEqual(job["skills"], [])
        for key in ("title", "company", "location", "contract_type", "description",
                    "publication_date", "salary", "experience", "url"):
            self.assertIsNone(job[key], key)
        self.assertEqual(normalize_job({
            "id": "123ABCD", "contact": {"urlPostulation": "https://example.org/postuler"}
        })["url"], "https://example.org/postuler")

    def test_missing_credentials_fail_before_any_network_call(self):
        client = self.make_client([], settings=Settings())
        with self.assertRaises(FranceTravailConfigurationError) as caught:
            client.search_jobs(location="Paris")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.requests, [])

    def test_limit_validation_before_network(self):
        for limit in (0, -1, 151, True, 1.5, "50"):
            with self.subTest(limit=limit), self.assertRaises(InvalidSearchError):
                self.make_client([]).search_jobs(limit=limit)
        self.assertEqual(self.requests, [])

    def test_absent_results_and_token_reuse(self):
        client = self.make_client([
            self.token(), httpx.Response(204), httpx.Response(200, json={"resultats": []}),
        ])
        self.assertEqual(client.search_jobs(), [])
        self.assertEqual(client.search_jobs(), [])
        self.assertEqual([request.method for request in self.requests], ["POST", "GET", "GET"])
        self.assertEqual(dict(self.requests[-1].url.params), {"range": "0-49"})

    def test_expired_token_is_renewed(self):
        client = self.make_client([
            self.token("first", 10), httpx.Response(204), self.token("second"), httpx.Response(204),
        ])
        with patch("app.france_travail.time.monotonic", return_value=100):
            client.search_jobs()
        with patch("app.france_travail.time.monotonic", return_value=111):
            client.search_jobs()
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer second")

    def test_unauthorized_search_retries_with_new_token_once(self):
        client = self.make_client([
            self.token("old"), httpx.Response(401), self.token("new"), httpx.Response(204),
        ])
        self.assertEqual(client.search_jobs(), [])
        self.assertEqual(self.requests[1].headers["Authorization"], "Bearer old")
        self.assertEqual(self.requests[3].headers["Authorization"], "Bearer new")

    def test_second_unauthorized_response_stops_retries(self):
        client = self.make_client([
            self.token("old"), httpx.Response(401), self.token("new"), httpx.Response(401),
        ])
        with self.assertRaises(FranceTravailConfigurationError):
            client.search_jobs()
        self.assertEqual(len(self.requests), 4)

    def test_http_errors_do_not_reveal_upstream_bodies(self):
        for status, error_type, local_status in (
            (400, InvalidSearchError, 422), (403, FranceTravailConfigurationError, 503),
            (429, FranceTravailError, 503), (500, FranceTravailError, 502),
        ):
            with self.subTest(status=status):
                client = self.make_client([self.token(), httpx.Response(status, text="secret-token-response")])
                with self.assertRaises(error_type) as caught:
                    client.search_jobs()
                self.assertEqual(caught.exception.status_code, local_status)
                self.assertNotIn("secret-token-response", str(caught.exception))

    def test_token_failure_and_malformed_authentication(self):
        responses = [
            httpx.Response(401, text="test-secret"), httpx.Response(200, text="not json"),
            httpx.Response(200, json=[]), httpx.Response(200, json={"access_token": "token"}),
            self.token(expires_in="NaN"), self.token(expires_in=True), self.token(expires_in=-1),
        ]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(FranceTravailError) as caught:
                self.make_client([response]).search_jobs()
            self.assertNotIn("test-secret", str(caught.exception))

    def test_timeout_is_actionable_and_does_not_leak_details(self):
        client = self.make_client([httpx.ReadTimeout("test-secret")])
        with self.assertRaises(FranceTravailError) as caught:
            client.search_jobs()
        self.assertEqual(caught.exception.status_code, 504)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_malformed_search_responses_are_reported(self):
        for payload in ({}, [], {"resultats": None}, {"resultats": [{}]}, {"resultats": ["bad"]}):
            with self.subTest(payload=payload), self.assertRaises(FranceTravailError):
                self.make_client([self.token(), httpx.Response(200, json=payload)]).search_jobs()
        with self.assertRaises(FranceTravailError):
            self.make_client([self.token(), httpx.Response(200, text="<html>bad</html>")]).search_jobs()

    def test_insee_code_skips_geographic_lookup_including_corsica(self):
        for location in ("33063", "2a004"):
            with self.subTest(location=location):
                client = self.make_client([self.token(), httpx.Response(204)])
                client.search_jobs(location=location)
                self.assertEqual(self.requests[-1].url.params["commune"], location.upper())
                self.assertEqual(self.requests[-2].url.host, "entreprise.francetravail.fr")

    def test_commune_normalization_and_exact_match(self):
        client = self.make_client([
            httpx.Response(200, json=[
                {"nom": "Saint-Étienne-de-Montluc", "code": "44158"},
                {"nom": "Saint-Étienne", "code": "42218"},
            ]), self.token(), httpx.Response(204),
        ])
        client.search_jobs(location="saint etienne")
        self.assertEqual(self.requests[-1].url.params["commune"], "42218")

    def test_ambiguous_commune_proposes_insee_codes(self):
        client = self.make_client([httpx.Response(200, json=[
            {"nom": "Saint-Sauveur", "code": "31516"},
            {"nom": "Saint-Sauveur", "code": "60597"},
        ])])
        with self.assertRaises(InvalidSearchError) as caught:
            client.search_jobs(location="Saint-Sauveur")
        self.assertIn("31516", str(caught.exception))
        self.assertIn("60597", str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    def test_unknown_commune_and_malformed_geographic_response(self):
        with self.assertRaises(InvalidSearchError):
            self.make_client([httpx.Response(200, json=[])]).search_jobs(location="Ville Inconnue")
        with self.assertRaises(FranceTravailError):
            self.make_client([httpx.Response(200, json={})]).search_jobs(location="Paris")
        with self.assertRaises(InvalidSearchError):
            self.make_client([]).search_jobs(location="75")


if __name__ == "__main__":
    unittest.main()
