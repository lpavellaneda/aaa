import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import cliente as c
from tests import test_ai_block as fixtures
from tests.test_cliente import FakeClient
measure = fixtures.measure


class ExportForceTests(unittest.TestCase):
    setUp = fixtures.BlockTests.setUp
    tearDown = fixtures.BlockTests.tearDown
    plan = fixtures.BlockTests.plan
    writes = fixtures.BlockTests.writes
    def forced_plan(self):
        self.client.catalog['measure_operations']['Ventas', 'Dep'] = dict(name='Dep', tableName='Ventas', expression='[Base] + 1')
        return self.plan([dict(measure('delete_measure', 'Base'), force=True)])

    def test_full_raw_export_preserves_unknown_properties_and_formats(self):
        client = FakeClient()
        client.responses['measure_operations', 'Get'] = {'data': [dict(name='Total', tableName='Ventas', expression='1',
            formatString='#,0.00', annotations=[{'key': 'test', 'value': 'valor'}], futureProperty={'raw': True})]}
        exported = c.metadata_export(client)
        self.assertTrue(exported['complete'], exported['errors'])
        self.assertEqual(exported['measures'][0]['futureProperty'], {'raw': True})
        self.assertEqual(exported['measures'][0]['formatString'], '#,0.00')
        self.assertTrue(exported['calculatedColumns'])
        self.assertTrue(exported['relationships'])

    def test_partial_export_explicit(self):
        self.client.fail_name = None
        with patch.object(c, 'get_object', side_effect=RuntimeError('No disponible')):
            result = c.metadata_export(self.client, 'measures')
        self.assertFalse(result['complete'])
        self.assertTrue(result['errors'])
        self.assertTrue(result['measures'][0]['metadataIncomplete'])

    def test_export_file_utf8_roundtrip(self):
        self.client.export_dir = self.client.backup_dir
        result = c.print_metadata_export(self.client, 'measures')
        path = next(Path(self.client.export_dir).glob('metadatos_*.json'))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), result)

    def test_measures_menu_shows_detail_once(self):
        with patch('builtins.input', side_effect=['1', '']), patch.object(c, 'show_details') as show:
            c.simple_list(self.client, 'measure_operations', 'MEDIDAS')
        show.assert_called_once()

    def test_tables_json_and_measures_json(self):
        for tool, expected in [('table_operations', 'all'), ('measure_operations', 'measures')]:
            with patch('builtins.input', side_effect=['json', '']), patch.object(c, 'print_metadata_export') as export:
                c.simple_list(self.client, tool, 'LISTADO')
            export.assert_called_once_with(self.client, expected)

    def test_force_requires_boolean_and_only_delete(self):
        for op in [dict(measure('delete_measure', 'Base'), force='true'), dict(measure(), force=True)]:
            with self.assertRaises(ValueError):
                c.parse_ai_block(json.dumps(op))

    def test_force_multiple_delete_expansion(self):
        result = c.parse_ai_block(json.dumps(dict(action='delete_measures', table='Ventas', names=['A', 'B'], force=True)))
        self.assertEqual([r['name'] for r in result], ['A', 'B'])
        self.assertTrue(all(r['force'] for r in result))

    def test_force_dependencies_warn_and_enable_cascade(self):
        plan = self.forced_plan()
        self.assertTrue(plan[0]['fields']['shouldCascadeDelete'])
        report = {'engine': True, 'warnings': [], 'rows': [{'[OBJECT_TYPE]': 'MEASURE', '[TABLE]': 'Ventas', '[OBJECT]': 'Dep',
                  '[REFERENCED_OBJECT_TYPE]': 'MEASURE', '[REFERENCED_TABLE]': 'Ventas', '[REFERENCED_OBJECT]': 'Base'}]}
        c.check_engine_dependencies(plan, report)
        self.assertTrue(report['warnings'])

    def test_force_without_ack_cannot_execute(self):
        with self.assertRaisesRegex(RuntimeError, 'CONFIRMAR FORZAR'):
            c.execute_ai_plan(self.client, self.forced_plan())
        self.assertFalse(self.writes())

    def test_cancel_force_cancels_entire_block(self):
        op = dict(measure('delete_measure', 'Base'), force=True)
        with patch('builtins.input', side_effect=[json.dumps(op), 'FIN', 'NO']):
            self.assertEqual(c.ai_block(self.client), [])
        self.assertFalse(self.writes())

    def test_readonly_force_rejected(self):
        self.client.writable = False
        with self.assertRaises(ValueError):
            self.forced_plan()
        self.assertFalse(self.writes())

    def test_force_without_transactions_rejected(self):
        plan = self.forced_plan()
        plan[0]['forceAuthorized'] = True
        with self.assertRaisesRegex(RuntimeError, 'transacciones'):
            c.execute_ai_plan(self.client, plan)
        self.assertFalse(self.writes())

    def test_force_missing_target_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'inexistente'):
            self.plan([dict(measure('delete_measure', 'Inexistente'), force=True)])

    def test_force_source_column_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'origen'):
            self.plan([dict(action='delete_column', table='Ventas', name='Origen', force=True)])

    def test_force_relationship_dependency_allowed(self):
        self.client.relationships = [dict(fromTable='Ventas', fromColumn='Calculada', toTable='Otra', toColumn='X')]
        plan = self.plan([dict(action='delete_column', table='Ventas', name='Calculada', force=True)])
        self.assertTrue(plan[0]['fields']['shouldCascadeDelete'])

    def test_snapshot_detects_collateral_and_semantic_error(self):
        before = {'measures': [dict(name='M', tableName='T', expression='1', state='Ready')]}
        after = copy.deepcopy(before)
        after['measures'][0]['state'] = 'SemanticError'
        self.assertTrue(c.snapshot_differences(before, after))
        self.assertTrue(c.snapshot_differences(before, {'measures': []}))


