"""Run offline: python -m unittest -v test_company_filter."""
import unittest
from unittest.mock import patch
import main


def job(company, level='senior'):
    return {'company': company, 'title': 'Senior Project Manager',
            'experience_level': level, 'remote': True, 'date': 2,
            'locations': [{'country': 'France'}]}


class CompanyFilterTests(unittest.TestCase):
    def search(self, rows, **params):
        with patch.object(main, 'load_jobs', return_value=rows):
            return main.get_jobs({'q': 'Senior Project Manager', **params})

    def test_meta_excludes_substring_companies(self):
        rows = [job(name) for name in ('metaforms', 'bigdmetal', 'imperialmetal',
                                      'nametag', 'metaview', 'Meta')]
        result = self.search(rows, company='Meta')
        self.assertEqual([j['company'] for j in result['jobs']], ['Meta'])
        self.assertEqual(result['total_matched'], 1)

    def test_case_and_surrounding_whitespace(self):
        rows = [job('  META '), job('meta'), job('Metaforms')]
        self.assertEqual(self.search(rows, company=' MeTa ')['count'], 2)

    def test_comma_separated_exact_names(self):
        rows = [job('Meta'), job('Netflix'), job('Metaforms'), job('netflixgames')]
        result = self.search(rows, company='meta, NETFLIX')
        self.assertEqual({j['company'] for j in result['jobs']}, {'Meta', 'Netflix'})

    def test_no_exact_match_returns_empty(self):
        result = self.search([job('Metaforms'), job('Metaview')], company='Meta')
        self.assertEqual(result['jobs'], [])
        self.assertEqual(result['total_matched'], 0)

    def test_no_company_filter_is_unrestricted(self):
        rows = [job('Meta'), job('Metaforms'), job(None)]
        self.assertEqual(self.search(rows)['count'], 3)

    def test_other_filters_still_intersect(self):
        result = self.search([job('Meta'), job('Meta', 'junior'), job('Metaforms')],
                             company='meta', exp='senior', country='france', remote='true', days='7')
        self.assertEqual(result['jobs'], [job('Meta')])

    def test_count_and_pagination_only_include_exact_matches(self):
        rows = [job('Metaforms'), job('Meta'), job('Meta'), job('Metaview')]
        result = self.search(rows, company='meta', amount='1', offset='1')
        self.assertEqual(result['total_matched'], 2)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['jobs'][0]['company'], 'Meta')

    def test_missing_company_never_matches(self):
        self.assertEqual(self.search([job(None), job('')], company='meta')['count'], 0)

if __name__ == '__main__':
    unittest.main()
