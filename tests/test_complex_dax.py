import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cliente as c
from tests.test_ai_block import ModelClient, measure
from tests.test_recovery import TransactionalClient


class ComplexDaxTests(unittest.TestCase):
    def setUp(self):
        self.client = ModelClient()
        self.temp = tempfile.TemporaryDirectory()
        self.client.backup_dir = Path(self.temp.name)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temp.cleanup()

    def plan(self, ops):
        return c.validate_ai_block(self.client, c.parse_ai_block(json.dumps({'operations': ops})))[0]

    def test_return_measure_is_unqualified(self):
        self.assertEqual(c.dax_references('VAR escala = 2 RETURN [Base] * escala'), {(None, 'base')})

    def test_multiline_vars_return_and_qualified_columns(self):
        refs = c.dax_references("VAR x = SUM('Ventas'[Importe])\nVAR y = [Base]\nRETURN DIVIDE(x,y,0)")
        self.assertEqual(refs, {('ventas', 'importe'), (None, 'base')})

    def test_quoted_keyword_table_is_not_removed(self):
        self.assertEqual(c.dax_references("SUM('RETURN'[Importe]) + [Base]"), {('return', 'importe'), (None, 'base')})

    def test_not_keyword_is_not_a_table(self):
        self.assertEqual(c.dax_references('NOT [Base]'), {(None, 'base')})

    def test_return_forward_reference_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'despues'):
            self.plan([measure(name='A', expression='VAR n=2 RETURN [B]*n'), measure(name='B')])

    def test_return_dependent_delete_inside_same_block_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'dependencia'):
            self.plan([measure(name='A', expression="SUM('Ventas'[Importe])"),
                       measure(name='B', expression='VAR n=2 RETURN [A]*n'), measure('delete_measure', name='A')])

    def test_return_indirect_cycle_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'circular'):
            self.plan([measure('update_measure', 'Base', expression='VAR n=2 RETURN [Vieja]*n'),
                       measure('update_measure', 'Vieja', 'Otra', 'VAR n=3 RETURN [Base]*n')])

    def test_valid_aggregations_and_vars_keep_order(self):
        expressions = ["SUM('Ventas'[Importe])", "AVERAGE('Ventas'[Importe])", "COUNTROWS('Ventas')",
                       "DISTINCTCOUNT('Ventas'[Id])", "SUMX('Ventas', 'Ventas'[Importe]*2)",
                       "CALCULATE([M0], FILTER(ALL('Ventas'), 'Ventas'[Importe]>0))",
                       "CALCULATE([M0], REMOVEFILTERS('Ventas'))", 'VAR n=2 RETURN [M0]*n']
        plan = self.plan([measure(name=f'M{i}', expression=expr) for i, expr in enumerate(expressions)])
        self.assertEqual([item['instruction']['expression'] for item in plan], expressions)

    def test_semantic_failure_after_commit_identifies_operation_and_recovers(self):
        client = TransactionalClient()
        client.backup_dir = Path(self.temp.name)
        self.client = client
        original = client.tool
        def semantic_at_commit(tool, args):
            value = original(tool, args)
            if tool == 'transaction_operations' and args['request']['operation'] == 'Commit':
                client.catalog['measure_operations']['Ventas', 'Mala'].update(state='SemanticError', errorMessage='Column does not exist')
            return value
        plan = self.plan([measure(name='Buena'), measure(name='Mala', expression="SUM('Ventas'[Ausente])")])
        with patch.object(client, 'tool', side_effect=semantic_at_commit):
            result = c.execute_ai_plan(client, plan)
        self.assertEqual(result[1]['status'], 'fallida')
        self.assertIn('Operacion 2', result[1]['error'])
        self.assertIn('Ventas[Mala]', result[1]['error'])
        self.assertEqual(client.last_recovery, 'verificada')
        self.assertIn('Fallidas: 1', self.output.getvalue())

    def test_column_storage_is_calculated_before_query(self):
        original = self.client.tool
        pending = [False]
        events = []
        explicit_query = 'EVALUATE ROW("V", SUM(Ventas[NuevaCol]))'
        def require_calculation(tool, args):
            request = args['request']
            operation = request['operation']
            events.append((tool, operation, request.get('query')))
            if tool == 'table_operations' and operation == 'RefreshWithXMLA':
                self.assertEqual(request['refreshType'], 'Calculate')
                pending[0] = False
                self.client.catalog['column_operations']['Ventas', 'NuevaCol']['state'] = 'Ready'
            if tool == 'dax_query_operations' and pending[0]:
                raise RuntimeError('Column does not hold data because it needs recalculation')
            result = original(tool, args)
            if tool == 'column_operations' and operation in ('Create', 'Update'):
                pending[0] = True
                self.client.catalog['column_operations']['Ventas', 'NuevaCol']['state'] = 'CalculationNeeded'
            return result
        plan = self.plan([measure('create_column', 'NuevaCol', expression='[Origen] * 2'),
                          {'action':'execute_dax','query':explicit_query}])
        with patch.object(self.client, 'tool', side_effect=require_calculation):
            c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.client.last_block_status, 'completado')
        refresh_index = next(i for i,e in enumerate(events) if e[1]=='RefreshWithXMLA')
        query_index = next(i for i,e in enumerate(events) if e[2]==explicit_query)
        self.assertLess(refresh_index, query_index)

    def test_native_columns_calculate_after_commit(self):
        client = TransactionalClient()
        client.backup_dir = Path(self.temp.name)
        self.client = client
        plan = self.plan([measure('create_column', 'NuevaCol', expression='[Origen] * 2')])
        c.execute_ai_plan(client, plan)
        events = [args['request']['operation'] for tool,args in client.calls]
        self.assertLess(events.index('Commit'), events.index('RefreshWithXMLA'))
        self.assertEqual(client.last_block_status,'completado')

    def test_failed_calculation_restores_original_column_and_recalculates(self):
        original = self.client.tool
        attempts = [0]
        def fail_first_calculate(tool,args):
            if tool=='table_operations' and args['request']['operation']=='RefreshWithXMLA':
                attempts[0]+=1
                if attempts[0]==1:
                    raise RuntimeError('Invalid calculation')
            return original(tool,args)
        plan = self.plan([measure('update_column','Calculada',expression='[Origen] * 3')])
        with patch.object(self.client,'tool',side_effect=fail_first_calculate):
            result=c.execute_ai_plan(self.client,plan)
        self.assertEqual(result[0]['status'],'fallida')
        self.assertEqual(self.client.last_recovery,'verificada')
        self.assertEqual(self.client.catalog['column_operations']['Ventas','Calculada']['expression'],'1')
        self.assertEqual(attempts[0],2)

    def test_column_reference_quotes_real_identifiers(self):
        self.assertEqual(c.dax_column_reference("O'Brien",'A]B'),"'O''Brien'[A]]B]")

    def test_metadata_restored_without_column_data_is_not_verified(self):
        original = self.client.tool
        def broken_storage(tool,args):
            if tool=='table_operations' and args['request']['operation']=='RefreshWithXMLA':
                raise RuntimeError('Calculate no disponible')
            if tool=='dax_query_operations' and '__MCP_ColumnReady' in args['request'].get('query',''):
                raise RuntimeError('Column needs to be recalculated')
            return original(tool,args)
        plan=self.plan([measure('update_column','Calculada',expression='[Origen] * 3')])
        with patch.object(self.client,'tool',side_effect=broken_storage):
            c.execute_ai_plan(self.client,plan)
        self.assertEqual(self.client.catalog['column_operations']['Ventas','Calculada']['expression'],'1')
        self.assertEqual(self.client.last_recovery,'incompleta')

    def test_deleted_column_is_recreated_and_calculated_during_recovery(self):
        self.client.fail_name='Mala'
        plan=self.plan([measure('delete_column','Calculada'),measure(name='Mala')])
        c.execute_ai_plan(self.client,plan)
        self.assertEqual(self.client.last_recovery,'verificada')
        self.assertEqual(self.client.catalog['column_operations']['Ventas','Calculada']['expression'],'1')
        restored=[args['request'] for tool,args in self.client.calls if tool=='column_operations' and args['request']['operation']=='Create']
        self.assertEqual(restored[0]['definitions'][0]['name'],'Calculada')
        self.assertTrue(any(args['request']['operation']=='RefreshWithXMLA' for tool,args in self.client.calls))

    def test_invalid_second_column_is_attributed_before_table_calculation(self):
        client=TransactionalClient()
        client.backup_dir=Path(self.temp.name)
        self.client=client
        original=client.tool
        def fail_semantics(tool,args):
            result=original(tool,args)
            if tool=='transaction_operations' and args['request']['operation']=='Commit':
                client.catalog['column_operations']['Ventas','BadCol'].update(state='SemanticError',errorMessage='Missing column')
            return result
        plan=self.plan([measure('create_column','GoodCol',expression='[Origen]*2'),measure('create_column','BadCol',expression='[Ausente]')])
        with patch.object(client,'tool',side_effect=fail_semantics):
            result=c.execute_ai_plan(client,plan)
        self.assertEqual(result[1]['status'],'fallida')
        self.assertIn('Operacion 2',result[1]['error'])
        self.assertEqual(client.last_recovery,'verificada')


if __name__ == '__main__':
    unittest.main()
