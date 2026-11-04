import unittest
from unittest.mock import patch
import main
from scrappy.codes.geoloc import canonicalize_country, extract_locations

class CountryTests(unittest.TestCase):
    def test_free_text_state_codes_are_not_country_codes(self):
        for raw in ['Somerville, MA', 'Somerville, ma', 'Jamaica Plain-MA', 'Waltham-MA', 'Newton-MA (office)', 'MA - Somerville', 'West Lafayette, IN', 'Wilmington, DE', 'Austin, TX 78701']:
            with self.subTest(raw=raw):
                self.assertEqual(extract_locations([raw])[0]['country'], 'United States')

    def test_morocco_and_explicit_country_fields_still_work(self):
        self.assertEqual(canonicalize_country('MA'), 'Morocco')
        for raw in ['Rabat, MA', 'Casablanca, Morocco', 'Tangier', 'Maroc']:
            with self.subTest(raw=raw):
                self.assertEqual(extract_locations([raw])[0]['country'], 'Morocco')
        self.assertEqual(extract_locations(['Toronto, CA'])[0]['country'], 'Canada')
        self.assertEqual(extract_locations(['Sao Luis / MA / Brazil'])[0]['country'], 'Brazil')
        for raw in ['Mar del Plata', 'Del Mar', 'Remote in Europe']:
            self.assertNotIn('Morocco', [x.get('country') for x in extract_locations([raw])])

    def test_api_exact_alias_matching_and_pagination(self):
        rows = [{'company':'Example','title':'Engineer','locations':loc} for loc in [
            [{'country':'United States','city':'Somerville'}], [{'country':'MA','city':'Rabat'}],
            [{'country':'Morocco','city':'Casablanca'}], 'Somerville, MA',
            [{'country':'Germany'}], [{'country':'Ireland'}], [{'country':'United Kingdom'}],
            [{'country':'France'}, {'country':'Morocco'}]]]
        with patch.object(main, 'load_jobs', return_value=rows):
            for country in ['Morocco','maroc','MA']:
                result=main.get_jobs({'q':'Engineer','country':country,'amount':'1','offset':'1'})
                self.assertEqual(result['total_matched'],3)
                self.assertEqual(result['jobs'],[rows[2]])
            self.assertEqual(main.get_jobs({'q':'Engineer','country':'USA'})['jobs'],[rows[0],rows[3]])
            self.assertEqual(main.get_jobs({'q':'Engineer','country':'land'})['count'],0)
            self.assertEqual(main.get_jobs({'q':'Engineer'})['count'],len(rows))

if __name__=='__main__':unittest.main()
