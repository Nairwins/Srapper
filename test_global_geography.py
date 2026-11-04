import json
import unittest
from pathlib import Path
from location_identity import country_codes, country_id, country_name, reference
from scrappy.codes.geoloc import extract_locations

class GlobalGeographyTests(unittest.TestCase):
    def test_every_supported_country_code_and_name(self):
        countries=reference()[0]['countries']
        self.assertGreaterEqual(len(countries),249)
        for code,entry in countries.items():
            with self.subTest(code=code):
                self.assertEqual(country_id(code),code)
                self.assertEqual(country_codes({'country':code}),{code})
                self.assertEqual(country_id(entry['name']),code)
                self.assertEqual(country_name(code),entry['name'])

    def test_api_filter_for_every_country(self):
        from unittest.mock import patch
        import main
        rows=[{'title':'Engineer','company':'Example','locations':[{'country':code}]} for code in reference()[0]['countries']]
        with patch.object(main,'load_jobs',return_value=rows):
            for code,entry in reference()[0]['countries'].items():
                result=main.get_jobs({'q':'Engineer','country':code})
                self.assertEqual(result['total_matched'],1,code)
                self.assertEqual(result['jobs'][0]['locations'][0]['country'],code)

    def test_worldwide_address_context(self):
        cases={'Berlin, DE':'DE','Wilmington, DE':'US','Bengaluru, IN':'IN',
               'West Lafayette, IN':'US','Toronto, CA':'CA','Ontario, CA':'US',
               'Tbilisi, Georgia':'GE','Atlanta, Georgia':'US','Melbourne, VIC':'AU',
               'Melbourne, FL':'US','London, Ontario, Canada':'CA','Tokyo, JPN':'JP',
               'Paris, TX':'US','Paris, France':'FR','Cambridge, UK':'GB',
               'Sao Luis / MA / Brazil':'BR','Berlin (DE-BE)':'DE',
               'Remote - Guinea-Bissau':'GW','Remote - Türkiye':'TR'}
        for address,code in cases.items():
            with self.subTest(address=address):
                self.assertEqual(country_codes(address),{code})
                self.assertEqual(extract_locations([address])[0]['country'],country_name(code))

    def test_ambiguity_never_reappears_as_country_in_fallback(self):
        for address in ['Unknown City, MA','Unknown City, IN','Unknown City, CA','Georgia','Del Mar','Mar del Plata','Remote in Europe']:
            with self.subTest(address=address):
                self.assertEqual(country_codes(address),set())
                loc=extract_locations([address])[0]
                self.assertNotIn('country',loc)
                self.assertEqual(loc['city'],address)
        self.assertEqual(country_codes({'country':'Georgia'}),{'GE'})
        self.assertEqual(country_codes({'country':'IN'}),{'IN'})
        self.assertEqual(country_codes({'country':'US','country_code':'MA'}),set())

    def test_unicode_aliases_and_multiple_countries(self):
        self.assertEqual(country_codes({'country':'المغرب'}),{'MA'})
        self.assertEqual(country_codes('France, Germany'),{'FR','DE'})
        self.assertEqual({country_id(x['country']) for x in extract_locations(['France, Germany'])},{'FR','DE'})

if __name__=='__main__':unittest.main()
