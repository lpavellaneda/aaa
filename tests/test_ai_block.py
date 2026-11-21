import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

import cliente as c
from tests.test_cliente import FakeClient


class ModelClient(FakeClient):
    """Mutable fake; actual protocol errors and empty write responses are retained."""
    def __init__(self, writable=True):
        super().__init__(writable)
        self.catalog = {
            'measure_operations': {
                ('Ventas', 'Base'): {'name': 'Base', 'tableName': 'Ventas', 'expression': '1'},
                ('Otra', 'Vieja'): {'name': 'Vieja', 'tableName': 'Otra', 'expression': '2'},
            },
            'column_operations': {
                ('Ventas', 'Calculada'): {'name': 'Calculada', 'tableName': 'Ventas', 'columnType': 'Calculated', 'expression': '1'},
                ('Ventas', 'Origen'): {'name': 'Origen', 'tableName': 'Ventas', 'columnType': 'Data', 'sourceColumn': 'Origen'},
            },
        }
        self.fail_name = None
        self.lost_name = None
        self.relationships = []

    def tool(self, name, args):
        c.enforce_policy(self, name, args)
        req = args['request']
        op = req['operation']
        if name in ('connection_operations',):
            return super().tool(name, args)
        self.calls.append((name, copy.deepcopy(args)))
        if name == 'table_operations':
            return {'data': [{'name': 'Ventas'}, {'name': 'Otra'}]}
        if name == 'relationship_operations':
            return {'data': self.relationships}
        if name == 'dax_query_operations':
            return {'data': {'rows': [{'Resultado': 1}]}}
        catalog = self.catalog[name]
        if op == 'List':
            return {'data': copy.deepcopy(list(catalog.values()))}
        if op == 'Get':
            rows = []
            for ref in req['references']:
                key = (ref['tableName'], ref['name'])
                if key not in catalog:
                    return {'isError': True, 'content': [{'type': 'text', 'text': 'Object not found'}]}
                rows.append(copy.deepcopy(catalog[key]))
            return {'data': rows}
        objects = req.get('definitions', req.get('references'))
        for obj in objects:
            if obj['name'] == self.fail_name:
                return {'isError': True, 'content': [{'type': 'text', 'text': 'DAX invalido'}]}
            if obj['name'] == self.lost_name:
                raise TimeoutError('Resultado incierto')
            key = (obj['tableName'], obj['name'])
            if op == 'Create':
                catalog[key] = dict(obj)
                if name == 'column_operations':
                    catalog[key]['columnType'] = 'Calculated'
            elif op == 'Update':
                catalog[key].update(obj)
            elif op == 'Delete':
                del catalog[key]
        return {'content': [], 'isError': False}


def measure(action='create_measure', name='Nueva', table='Ventas', expression='1'):
    op = dict(action=action, name=name, table=table)
    if not action.startswith('delete_'):
        op['expression'] = expression
    return op


class BlockTests(unittest.TestCase):
    def setUp(self):
        self.client = ModelClient()
        self.output = io.StringIO()
        self.context = contextlib.redirect_stdout(self.output)
        self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def plan(self, ops):
        parsed = c.parse_ai_block(json.dumps({'operations': ops}))
        return c.validate_ai_block(self.client, parsed)[0]

    def writes(self):
        return [(tool, args['request']) for tool, args in self.client.calls
                if args['request']['operation'] in ('Create', 'Update', 'Delete')]

    def test_single_operation(self):
        parsed = c.parse_ai_block(json.dumps(measure()))
        plan, _ = c.validate_ai_block(self.client, parsed)
        result = c.execute_ai_plan(self.client, plan)
        self.assertEqual(result[0]['status'], 'correcta')
        self.assertIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])

    def test_hundreds_no_arbitrary_limit(self):
        plan = self.plan([measure(name=f'Nueva{i}') for i in range(300)])
        result = c.execute_ai_plan(self.client, plan)
        self.assertEqual(len(result), 300)
        self.assertTrue(all(r['status'] == 'correcta' for r in result))

    def test_multiple_updates_different_tables(self):
        plan = self.plan([measure('update_measure', 'Base', expression='10'),
                          measure('update_measure', 'Vieja', 'Otra', '20')])
        c.execute_ai_plan(self.client, plan)
        self.assertEqual([req['definitions'][0]['expression'] for _, req in self.writes()], ['10', '20'])

    def test_short_delete_expands_in_order(self):
        self.client.catalog['measure_operations']['Ventas', 'Segunda'] = {'tableName': 'Ventas', 'name': 'Segunda', 'expression': '1'}
        ops = c.parse_ai_block('{"action":"delete_measures","table":"Ventas","names":["Base","Segunda"]}')
        plan, _ = c.validate_ai_block(self.client, ops)
        c.execute_ai_plan(self.client, plan)
        self.assertEqual([r['references'][0]['name'] for _, r in self.writes()], ['Base', 'Segunda'])
        self.assertTrue(all(not r['shouldCascadeDelete'] for _, r in self.writes()))

    def test_mixed_create_update_delete_same_object(self):
        plan = self.plan([measure(), measure('update_measure', expression='2'), measure('delete_measure'),
                          measure('delete_measure', 'Vieja', 'Otra')])
        results = c.execute_ai_plan(self.client, plan)
        self.assertEqual(len(results), 4)
        self.assertEqual([r['operation'] for _, r in self.writes()], ['Create', 'Update', 'Delete', 'Delete'])

    def test_columns_create_update_delete(self):
        plan = self.plan([measure('create_column', 'NuevaCol'), measure('update_column', 'NuevaCol', expression='2'),
                          measure('delete_column', 'NuevaCol')])
        self.assertEqual(len(c.execute_ai_plan(self.client, plan)), 3)
        self.assertNotIn(('Ventas', 'NuevaCol'), self.client.catalog['column_operations'])

    def test_dax_in_read_mode_and_inline(self):
        self.client.writable = False
        plan = self.plan([{'action': 'execute_dax', 'query': 'EVALUATE ROW("x",1)', 'maxRows': 5}])
        self.assertEqual(c.execute_ai_plan(self.client, plan)[0]['status'], 'correcta')
        req = [r['request'] for tool, r in self.client.calls if tool == 'dax_query_operations'][0]
        self.assertEqual(req['resultMode'], 'Inline')
        self.assertEqual(req['connectionName'], 'conexion-prueba')

    def test_invalid_json_and_duplicate_keys(self):
        for text in ('{', '[]', '{"operations":[]}', '{"action":"a","action":"b"}',
                     '{"action":"execute_dax","query":NaN}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                c.parse_ai_block(text)

    def test_unknown_actions_and_connection_overrides(self):
        for op in ({'action': 'powershell', 'command': 'whoami'}, dict(measure(), connectionName='other'),
                   dict(measure(), model='other'), dict(measure(), version=1)):
            with self.subTest(op=op), self.assertRaises(ValueError):
                c.parse_ai_block(json.dumps(op))

    def test_invalid_types(self):
        for op in (dict(measure(), name=''), dict(measure(), expression=[]), dict(measure(), isHidden='false'),
                   {'action': 'execute_dax', 'query': 'EVALUATE x', 'maxRows': True},
                   {'action': 'delete_measures', 'table': 'Ventas', 'names': 'Base'},
                   {'action': 'update_measure', 'table': 'Ventas', 'name': 'Base'}):
            with self.subTest(op=op), self.assertRaises(ValueError):
                c.parse_ai_block(json.dumps(op))

    def test_missing_measure_and_table_no_partial_writes(self):
        for invalid in (measure('delete_measure', 'Ausente'), measure(table='Ausente')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.plan([measure(), invalid])
        self.assertEqual(self.writes(), [])

    def test_conflict_global_measure_names(self):
        with self.assertRaises(ValueError):
            self.plan([measure(name='BASE', table='Otra')])

    def test_duplicate_and_contradictory_ops(self):
        cases = ([measure(), measure()], [measure(), measure('delete_measure'), measure()],
                 [measure('update_measure', 'Base'), measure('update_measure', 'Base', expression='2')])
        for ops in cases:
            with self.subTest(ops=ops), self.assertRaises(ValueError):
                self.plan(ops)
        self.assertEqual(self.writes(), [])

    def test_case_canonicalization(self):
        plan = self.plan([measure('update_measure', 'base', 'ventas', '3')])
        c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.writes()[0][1]['definitions'][0]['name'], 'Base')

    def test_readonly_rejects_entire_mixed_plan(self):
        self.client.writable = False
        with self.assertRaises(ValueError):
            self.plan([{'action': 'execute_dax', 'query': 'EVALUATE x'}, measure()])
        self.assertEqual(self.client.calls, [])

    def test_no_connection(self):
        self.client.connection_name = None
        with self.assertRaisesRegex(ValueError, 'conexion'):
            self.plan([measure()])

    def test_delete_needs_exact_confirmation(self):
        lines = [json.dumps(measure('delete_measure', 'Base')), 'FIN', 'n', 's']
        with patch('builtins.input', side_effect=lines):
            result = c.ai_block(self.client)
        self.assertEqual(result, [])
        self.assertEqual(self.writes(), [])

    def test_one_confirmation_for_whole_block(self):
        ops = [measure(), measure('update_measure', expression='3'), measure('delete_measure')]
        with patch('builtins.input', side_effect=[json.dumps({'operations': ops}), 'FIN', 'n', 'CONFIRMAR']) as inp:
            results = c.ai_block(self.client)
        self.assertEqual(len(results), 3)
        self.assertEqual(sum('CONFIRMAR' in str(call) for call in inp.call_args_list), 1)
        self.assertIn('Modelo de destino: Prueba', self.output.getvalue())

    def test_failure_stops_remainder_preserves_previous(self):
        self.client.fail_name = 'Mala'
        result = c.execute_ai_plan(self.client, self.plan([measure(), measure(name='Mala'), measure(name='NoEnviada')]))
        self.assertEqual([r['status'] for r in result], ['correcta', 'fallida'])
        self.assertIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])
        self.assertNotIn(('Ventas', 'NoEnviada'), self.client.catalog['measure_operations'])
        self.assertIn('No ejecutadas: 1', self.output.getvalue())

    def test_timeout_never_retried(self):
        self.client.lost_name = 'Nueva'
        results = c.execute_ai_plan(self.client, self.plan([measure(), measure(name='OtraNueva')]))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.writes()), 1)
        self.assertIsNone(self.client.connection_name)

    def test_forward_dependency_rejected_prior_to_writes(self):
        with self.assertRaisesRegex(ValueError, 'despues'):
            self.plan([measure(name='Dependiente', expression='[Futura] + 1'), measure(name='Futura')])
        self.assertEqual(self.writes(), [])

    def test_references_to_deleted_objects_are_rejected(self):
        for reference in (measure(name='Depende', expression='[Base]'),
                          {'action': 'execute_dax', 'query': 'EVALUATE ROW("x", [Base])'}):
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError, 'eliminado'):
                self.plan([measure('delete_measure', 'Base'), reference])

    def test_query_before_measure_creation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'creado despues'):
            self.plan([{'action': 'execute_dax', 'query': 'EVALUATE ROW("x", [Futura])'}, measure(name='Futura')])

    def test_direct_self_reference_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'circular'):
            self.plan([measure('update_measure', 'Base', expression='[Base] + 1')])

    def test_indirect_measure_cycle_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'circular'):
            self.plan([measure('update_measure', 'Base', expression='[Vieja]'),
                       measure('update_measure', 'Vieja', 'Otra', '[Base]')])

    def test_correct_dependency_order_preserved(self):
        plan = self.plan([measure(name='Primera'), measure(name='Dependiente', expression='[Primera] + 1')])
        c.execute_ai_plan(self.client, plan)
        self.assertEqual([r['definitions'][0]['name'] for _, r in self.writes()], ['Primera', 'Dependiente'])

    def test_existing_dependency_blocks_delete(self):
        self.client.catalog['measure_operations']['Otra', 'Depende'] = {'name': 'Depende', 'tableName': 'Otra', 'expression': '[Base] + 1'}
        with self.assertRaisesRegex(ValueError, 'dependencia'):
            self.plan([measure('delete_measure', 'Base')])

    def test_update_dependency_then_delete_is_valid(self):
        self.client.catalog['measure_operations']['Otra', 'Depende'] = {'name': 'Depende', 'tableName': 'Otra', 'expression': '[Base] + 1'}
        plan = self.plan([measure('update_measure', 'Depende', 'Otra', '5'), measure('delete_measure', 'Base')])
        self.assertEqual(len(plan), 2)

    def test_imported_column_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'no compatible'):
            self.plan([measure('delete_column', 'Origen')])

    def test_relationship_blocks_column_delete(self):
        self.client.relationships = [{'name': 'r', 'fromTable': 'Ventas', 'fromColumn': 'Calculada', 'toTable': 'Otra', 'toColumn': 'Id'}]
        with self.assertRaisesRegex(ValueError, 'relacion'):
            self.plan([measure('delete_column', 'Calculada')])

    def test_no_partial_match_or_wildcard_expansion(self):
        for name in ('Ba', '*', 'Base?'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.plan([measure('delete_measure', name)])

    def test_lexical_scan_ignores_literals_and_comments(self):
        refs = c.dax_references('"[Falsa]" & [Real] // [Comentario]\n + \'Ventas\'[Importe] /* [Otra] */')
        self.assertEqual(refs, {(None, 'real'), ('ventas', 'importe')})

    def test_unavailable_schema_operation(self):
        self.client.tools['measure_operations']['inputSchema']['properties']['request']['properties']['operation']['description'] = 'Help, Get, List'
        with self.assertRaisesRegex(ValueError, 'no disponible'):
            self.plan([measure()])


if __name__ == '__main__':
    unittest.main()
