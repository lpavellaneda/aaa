import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import cliente as c
from tests import test_ai_block as fixtures
ModelClient = fixtures.ModelClient
measure = fixtures.measure


class TransactionalClient(ModelClient):
    def __init__(self):
        super().__init__()
        self.tools['transaction_operations'] = {'name': 'transaction_operations', 'inputSchema': {
            'type': 'object', 'properties': {'request': {'type': 'object', 'required': ['operation'],
            'properties': {'operation': {'type': 'string', 'enum': ['Begin', 'Commit', 'Rollback']},
                           'connectionName': {'type': 'string'}, 'transactionId': {'type': 'string'}}}}}}
        self.fail_rollback = False
        self.fail_commit = False
        self.tx = None

    def tool(self, name, args):
        if name != 'transaction_operations':
            return super().tool(name, args)
        self.calls.append((name, copy.deepcopy(args)))
        op = args['request']['operation']
        if op == 'Begin':
            self.tx = copy.deepcopy(self.catalog)
            return {'transactionId': 'tx-prueba'}
        if op == 'Rollback':
            if self.fail_rollback:
                raise RuntimeError('Rollback no disponible')
            self.catalog = self.tx
            self.tx = None
        if op == 'Commit':
            if self.fail_commit:
                raise RuntimeError('Commit rechazado')
            self.tx = None
        return {'content': [], 'isError': False}


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.BlockTests.setUp
    tearDown = fixtures.BlockTests.tearDown
    plan = fixtures.BlockTests.plan
    writes = fixtures.BlockTests.writes
    def test_backup_includes_previous_state_and_journal(self):
        result = c.execute_ai_plan(self.client, self.plan([measure('update_measure', 'Base', expression='3')]))
        self.assertEqual(result[0]['status'], 'correcta')
        data = json.loads(Path(self.client.last_backup_path).read_text(encoding='utf-8'))
        self.assertEqual(data['targets'][0]['before']['expression'], '1')
        self.assertEqual(data['journal'][0]['before']['expression'], '1')
        self.assertEqual(data['status'], 'completado')

    def test_compensation_restores_update_delete_and_create(self):
        before = c.stable_state(copy.deepcopy(self.client.catalog))
        self.client.fail_name = 'Mala'
        ops = [measure('update_measure', 'Base', expression='99'), measure('delete_measure', 'Vieja', 'Otra'),
               measure(name='Nueva'), measure(name='Mala')]
        c.execute_ai_plan(self.client, self.plan(ops))
        self.assertEqual(c.stable_state(self.client.catalog), before)
        self.assertEqual(self.client.last_recovery, 'verificada')

    def test_native_rollback_restores_state(self):
        temp = TransactionalClient()
        temp.backup_dir = self.client.backup_dir
        self.client = temp
        before = copy.deepcopy(self.client.catalog)
        self.client.fail_name = 'Mala'
        result = c.execute_ai_plan(self.client, self.plan([measure('delete_measure', 'Base'), measure(name='Mala')]))
        self.assertEqual(result[-1]['status'], 'fallida')
        self.assertEqual(self.client.catalog, before)
        self.assertEqual(self.client.last_recovery, 'verificada')

    def test_native_commit(self):
        temp = TransactionalClient()
        temp.backup_dir = self.client.backup_dir
        self.client = temp
        c.execute_ai_plan(self.client, self.plan([measure()]))
        self.assertIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])
        operations = [args['request']['operation'] for tool, args in self.client.calls if tool == 'transaction_operations']
        self.assertEqual(operations, ['Begin', 'Commit'])
        write = [args['request'] for tool, args in self.client.calls if args['request']['operation'] == 'Create'][0]
        self.assertFalse(write['options']['useTransaction'])

    def test_mixed_query_plan_uses_compensation_for_visibility(self):
        temp = TransactionalClient()
        temp.backup_dir = self.client.backup_dir
        self.client = temp
        plan = self.plan([measure(), {'action': 'execute_dax', 'query': 'EVALUATE ROW("x", [Nueva])'}])
        c.execute_ai_plan(self.client, plan)
        self.assertFalse(any(tool == 'transaction_operations' for tool, args in self.client.calls))
        self.assertEqual(self.client.last_block_status, 'completado')

    def test_inferred_type_change_on_commit_is_normal(self):
        temp = TransactionalClient()
        temp.backup_dir = self.client.backup_dir
        self.client = temp
        original = self.client.tool
        def commit_infers_type(name, args):
            value = original(name, args)
            if name == 'measure_operations' and args['request']['operation'] == 'Create':
                self.client.catalog[name]['Ventas', 'Nueva']['dataType'] = 'Unknown'
            if name == 'transaction_operations' and args['request']['operation'] == 'Commit':
                self.client.catalog['measure_operations']['Ventas', 'Nueva']['dataType'] = 'Int64'
            return value
        with patch.object(self.client, 'tool', side_effect=commit_infers_type):
            c.execute_ai_plan(self.client, self.plan([measure()]))
        self.assertEqual(self.client.last_block_status, 'completado')

    def test_commit_failure_triggers_recovery(self):
        temp = TransactionalClient()
        temp.backup_dir = self.client.backup_dir
        temp.fail_commit = True
        self.client = temp
        c.execute_ai_plan(self.client, self.plan([measure()]))
        self.assertNotIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])
        self.assertEqual(self.client.last_recovery, 'verificada')
        self.assertIn('Fallidas: 1', self.output.getvalue())
        self.assertEqual(self.client.last_block_status, 'fallido')

    def test_rollback_failure_does_not_claim_success_or_compensate_inside_transaction(self):
        temp = TransactionalClient()
        temp.backup_dir = self.client.backup_dir
        temp.fail_name = 'Mala'
        temp.fail_rollback = True
        self.client = temp
        c.execute_ai_plan(self.client, self.plan([measure(), measure(name='Mala')]))
        self.assertEqual(self.client.last_recovery, 'incompleta')
        self.assertIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])
        self.assertNotIn('RECUPERACION VERIFICADA', self.output.getvalue())

    def test_backup_failure_prevents_writes(self):
        plan = self.plan([measure()])
        with patch.object(c, 'save_recovery_file', side_effect=OSError('Disco lleno')), self.assertRaises(OSError):
            c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.writes(), [])

    def test_compensation_failure_is_reported_as_incomplete(self):
        self.client.fail_name = 'Mala'
        plan = self.plan([measure(), measure(name='Mala')])
        original = self.client.tool
        def fail_restore(tool, args):
            if args['request']['operation'] == 'Delete':
                raise RuntimeError('El servidor rechazo la compensacion')
            return original(tool, args)
        with patch.object(self.client, 'tool', side_effect=fail_restore):
            c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.client.last_recovery, 'incompleta')
        self.assertIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])

    def test_semantic_error_after_write_triggers_recovery(self):
        original = self.client.tool
        def semantic_error(tool, args):
            value = original(tool, args)
            if tool == 'measure_operations' and args['request']['operation'] == 'Create':
                self.client.catalog[tool]['Ventas', 'Nueva'].update(state='SemanticError', errorMessage='Formula DAX invalida')
            return value
        with patch.object(self.client, 'tool', side_effect=semantic_error):
            c.execute_ai_plan(self.client, self.plan([measure()]))
        self.assertEqual(self.client.last_block_status, 'fallido')
        self.assertEqual(self.client.last_recovery, 'verificada')
        self.assertNotIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])

    def test_engine_dependency_blocks_delete(self):
        plan = self.plan([measure('delete_measure', 'Base')])
        report = {'engine': True, 'rows': [{'[REFERENCED_OBJECT_TYPE]': 'MEASURE', '[REFERENCED_TABLE]': 'Ventas',
            '[REFERENCED_OBJECT]': 'Base', '[OBJECT_TYPE]': 'MEASURE', '[TABLE]': 'Otra', '[OBJECT]': 'Dependiente'}]}
        with self.assertRaisesRegex(ValueError, 'motor'):
            c.check_engine_dependencies(plan, report)

    def test_engine_dependency_updated_first(self):
        plan = self.plan([measure('update_measure', 'Vieja', 'Otra', '5'), measure('delete_measure', 'Base')])
        report = {'engine': True, 'rows': [{'[REFERENCED_OBJECT_TYPE]': 'MEASURE', '[REFERENCED_TABLE]': 'Ventas',
            '[REFERENCED_OBJECT]': 'Base', '[OBJECT_TYPE]': 'MEASURE', '[TABLE]': 'Otra', '[OBJECT]': 'Vieja'}]}
        c.check_engine_dependencies(plan, report)

    def test_unavailable_engine_dependency_report_is_explicit(self):
        report = c.dependency_report(self.client)
        self.assertFalse(report['engine'])
        self.assertTrue(any('No fue posible' in w for w in report['warnings']))
        self.assertTrue(any('visuales' in w for w in report['warnings']))


if __name__ == '__main__':
    unittest.main()
