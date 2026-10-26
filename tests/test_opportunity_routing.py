"""Vérifications ciblées de l'intégration ; aucun appel réseau réel."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import httpx
from app.config import Settings
from app.database import init_db, add_job
from app.applications import get_offer, save_notes
from app.labonneboite import LaBonneBoiteClient, search_companies, normalize_company
from app.opportunities import list_opportunities, upsert_opportunity
from app.application_router import send_application, detect_application_channel, recent_events, refresh_public_contact
from app.browser_automation.engine import prefill_page


class OpportunityChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = Settings(database_path=Path(self.folder.name) / 'test.db', france_travail_client_id='test', france_travail_client_secret='test')
        init_db(self.settings.database_path)

    def test_official_pagination_dedup_and_persistence(self):
        requests = []
        def respond(request):
            if request.method == 'POST':
                self.assertIn(b'api_labonneboitev2', request.content)
                return httpx.Response(200, json={'access_token': 'fake', 'expires_in': 3600})
            self.assertEqual(request.url.path, '/partenaire/labonneboite/v2/recherche')
            self.assertNotIn('location', request.url.params)
            page = int(request.url.params['page'])
            keyword = request.url.params['job']
            requests.append((keyword, page))
            indexes = range(100) if page == 1 else range(99, 102)
            return httpx.Response(200, json={'hits': 102, 'items': [{'siret': f'{i:014d}', 'company_name': f'Entreprise {i}'} for i in indexes]})
        with httpx.Client(transport=httpx.MockTransport(respond)) as http, patch('app.labonneboite.time.sleep'):
            report = search_companies(['Data Scientist', 'Data Engineer'], self.settings, limit=200, client=LaBonneBoiteClient(self.settings, http))
        self.assertEqual(requests, [('Data Scientist',1), ('Data Scientist',2), ('Data Engineer',1), ('Data Engineer',2)])
        self.assertEqual((report['raw_count'], report['unique_count']), (206, 102))
        rows = list_opportunities(self.settings.database_path)
        self.assertEqual(len(rows), 102)
        self.assertEqual(rows[0]['keywords'], ['Data Scientist', 'Data Engineer'])
        save_notes(rows[0]['external_id'], 'Note conservée', 'labonneboite', self.settings.database_path)
        init_db(self.settings.database_path)
        self.assertEqual(get_offer(rows[0]['external_id'], 'labonneboite', self.settings.database_path)['personal_notes'], 'Note conservée')

    def test_channels_and_no_invented_email(self):
        cases = [({'contact_email':'jobs@example.org'},'EMAIL'),
                 ({'url':'https://labonneboite.francetravail.fr/entreprise/123'},'LABONNEBOITE'),
                 ({'url':'https://candidat.francetravail.fr/offres/recherche/detail/123'},'FRANCE_TRAVAIL'),
                 ({'application_url':'https://jobs.lever.co/entreprise/poste'},'ATS'),
                 ({'website':'https://entreprise.example.org'},'COMPANY_SITE'),
                 ({'url':'https://annonces.example.org'},'MANUAL'), ({},'UNKNOWN')]
        for item, expected in cases:
            self.assertEqual(detect_application_channel(item)['channel'], expected)
        self.assertEqual(detect_application_channel({'application_url':'https://jobs.lever.co/x', 'contact_email':'a@example.org','contact_email_manual':1})['channel'], 'EMAIL')
        self.assertEqual(normalize_company({'siret':'12345678901234','company_name':'Exemple'}, 'Data')['contact_email'], '')

    def test_browser_route_traces_without_sent_and_simulation_has_no_io(self):
        item = normalize_company({'siret':'12345678901234'}, 'Data')
        upsert_opportunity(item, self.settings.database_path)
        with closing(sqlite3.connect(self.settings.database_path)) as db, db:
            db.execute("UPDATE opportunities SET application_status='Prête à envoyer', application_message='Message enregistré'")
        with patch('app.application_router.prepare_browser_application') as engine, patch('app.application_router.refresh_public_contact') as contact:
            result = send_application(item['external_id'],'labonneboite',self.settings,dry_run=True)
            engine.assert_not_called(); contact.assert_not_called()
            self.assertEqual(result['status'],'SIMULATION')
        with patch('app.application_router.prepare_browser_application', return_value={'status':'MANUAL_REVIEW_REQUIRED','message':'À vérifier','url':item['source_url']}) as engine:
            send_application(item['external_id'],'labonneboite',self.settings)
            engine.assert_called_once()
        row = get_offer(item['external_id'],'labonneboite',self.settings.database_path)
        self.assertEqual(row['application_status'], 'Intervention requise')
        self.assertIsNone(row['application_sent_at'])
        self.assertEqual(len(recent_events(self.settings.database_path)),3)

    def test_email_router_reuses_draft_and_records_failures(self):
        add_job({'external_id':'mail','contact_email':'jobs@example.org'}, self.settings.database_path)
        with closing(sqlite3.connect(self.settings.database_path)) as db, db:
            db.execute("UPDATE jobs SET application_status='Prête à envoyer',application_message='Message',gmail_draft_id='existing'")
        with patch('app.application_router.create_application_draft') as gmail:
            self.assertEqual(send_application('mail','france_travail',self.settings)['status'],'DRAFT_CREATED')
            gmail.assert_not_called()
        with closing(sqlite3.connect(self.settings.database_path)) as db, db:
            db.execute('UPDATE jobs SET gmail_draft_id=NULL')
        with patch('app.application_router.create_application_draft', side_effect=ValueError('Gmail absent')):
            self.assertEqual(send_application('mail','france_travail',self.settings)['status'],'ERROR')
        row=get_offer('mail',db_path=self.settings.database_path)
        self.assertEqual(row['application_error'],'Gmail absent')
        self.assertIsNone(row['application_sent_at'])

    def test_public_contacts_cache_and_manual_override(self):
        add_job({'external_id':'site','website':'https://example.org'},self.settings.database_path)
        row=get_offer('site',db_path=self.settings.database_path)
        with patch('app.public_contacts.find_public_contact',return_value=[{'email':'jobs@example.org','source_url':'https://example.org/contact'}]) as search:
            row=refresh_public_contact(row,self.settings)
            row=refresh_public_contact(row,self.settings)
            search.assert_called_once()
        self.assertEqual(row['contact_email'],'jobs@example.org')
        with closing(sqlite3.connect(self.settings.database_path)) as db, db:
            db.execute("UPDATE jobs SET contact_email='',contact_email_manual=1")
        add_job({'external_id':'site','contact_email':'new@example.org'},self.settings.database_path)
        self.assertEqual(get_offer('site',db_path=self.settings.database_path)['contact_email'],'')

    def test_captcha_stops_before_any_form_interaction(self):
        page=MagicMock()
        page.locator.return_value.inner_text.return_value='CAPTCHA : verify you are human'
        page.locator.return_value.count.return_value=0
        result=prefill_page(page,{'email':'candidate@example.org'},'Message',Path('absent.pdf'))
        self.assertIn('Aucun champ rempli',result)
        page.get_by_label.assert_not_called()
        page.locator.return_value.fill.assert_not_called()
        page.locator.return_value.set_input_files.assert_not_called()
