"""Pruebas simuladas; tool_schemas.json procede del servidor 1.0.1 instalado."""
import contextlib
import copy
import io
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch

import cliente as c
from codificar import encode_bytes, encode_file
from decodificar import decode_bytes, decode_file

SCHEMAS = json.loads(Path(__file__).with_name('tool_schemas.json').read_text(encoding='utf-8'))
INSTANCE = {'port': 54321, 'parentWindowTitle': 'Prueba',
            'connectionString': 'Data Source=localhost:54321;Application Name=PBIModeling'}


class FakeClient:
    def __init__(self, writable=False):
        self.tools = copy.deepcopy(SCHEMAS)
        self.writable = writable
        self.connection_name = 'conexion-prueba'
        self.model_name = 'Prueba'
        self.instance = dict(INSTANCE)
        self.calls = []
        self.responses = {}

    def tool(self, name, args):
        c.enforce_policy(self, name, args)
        self.calls.append((name, copy.deepcopy(args)))
        op = args['request']['operation']
        if (name, op) in self.responses:
            result = self.responses[name, op]
            if callable(result):
                return result(args)
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)
        if op == 'ListLocalInstances':
            return {'data': [dict(INSTANCE)]}
        if op == 'GetConnection':
            return {'data': {'connectionName': self.connection_name}}
        if op == 'Connect':
            return {'data': 'nueva-conexion'}
        if op == 'List':
            if name == 'table_operations':
                return {'data': [{'name': 'Ventas'}]}
            if name == 'relationship_operations':
                return {'data': [{'name': 'Relacion', 'fromTable': 'A', 'fromColumn': 'Id', 'toTable': 'B', 'toColumn': 'Id'}]}
            kind = 'measures' if name == 'measure_operations' else 'columns'
            return {'data': [{'tableName': 'Ventas', kind: [{'name': 'Total', 'isCalculated': True}]}]}
        if op == 'Get':
            ref = args['request']['references'][0]
            return {'data': [{'success': True, 'data': dict(ref, expression='1', columnType='Calculated', description='Actual')} ]}
        return {'success': True, 'message': 'Completed successfully'}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.client = FakeClient()

    def tearDown(self):
        self.stdout.__exit__(None, None, None)

    def test_instances(self):
        self.assertEqual(c.list_instances(self.client), [INSTANCE])
        self.assertNotIn('connectionName', self.client.calls[-1][1]['request'])

    def test_connect_string_and_no_old_connection(self):
        with patch('builtins.input', return_value='1'):
            c.connect(self.client)
        self.assertEqual(self.client.connection_name, 'nueva-conexion')
        self.assertEqual(self.client.model_name, 'Prueba')
        req = next(args['request'] for tool, args in self.client.calls if args['request']['operation'] == 'Connect')
        self.assertEqual(req['connectionString'], INSTANCE['connectionString'])
        self.assertNotIn('connectionName', req)

    def test_connect_object_response(self):
        self.client.responses['connection_operations', 'Connect'] = {'data': {'connectionName': 'objeto'}}
        with patch('builtins.input', return_value='1'):
            c.connect(self.client)
        self.assertEqual(self.client.connection_name, 'objeto')

    def test_cancel_keeps_active_connection(self):
        with patch('builtins.input', return_value='0'):
            c.connect(self.client)
        self.assertEqual(self.client.connection_name, 'conexion-prueba')

    def test_invalid_model_selection(self):
        with patch('builtins.input', return_value='20'), self.assertRaises(ValueError):
            c.connect(self.client)

    def test_no_open_models(self):
        self.client.responses['connection_operations', 'ListLocalInstances'] = {'data': []}
        c.connect(self.client)
        self.assertIsNone(self.client.connection_name)
        self.assertIn('Abre Power BI Desktop', self.output.getvalue())

    def test_bad_connect_clears_previous_identity(self):
        self.client.responses['connection_operations', 'Connect'] = {'message': 'pending'}
        with patch('builtins.input', return_value='1'), self.assertRaises(RuntimeError):
            c.connect(self.client)
        self.assertIsNone(self.client.connection_name)

    def test_missing_connection(self):
        self.client.connection_name = None
        self.assertFalse(c.ensure_connected(self.client))
        with self.assertRaises(RuntimeError):
            c.request_call(self.client, 'table_operations', 'List')

    def test_closed_model(self):
        self.client.responses['connection_operations', 'ListLocalInstances'] = {'data': []}
        self.assertFalse(c.ensure_connected(self.client))
        self.assertIsNone(self.client.connection_name)

    def test_connection_error(self):
        self.client.responses['connection_operations', 'GetConnection'] = RuntimeError('Connection lost')
        self.assertFalse(c.ensure_connected(self.client))
        self.assertIsNone(self.client.connection_name)

    def test_reused_port_does_not_reuse_process_identity(self):
        self.client.instance['processId'] = 12
        self.client.responses['connection_operations', 'ListLocalInstances'] = {'data': [dict(INSTANCE, processId=13)]}
        self.assertFalse(c.ensure_connected(self.client))

    def test_tables_without_technical_prompts(self):
        with patch('builtins.input', return_value='') as inp:
            c.simple_list(self.client, 'table_operations', 'TABLAS')
        self.assertEqual(inp.call_count, 1)  # Optional structure selector only.
        self.assertIn('Ventas', self.output.getvalue())
        self.assertNotIn('connectionName', self.output.getvalue())

    def test_grouped_measures_keep_table(self):
        rows = c.list_objects(self.client, 'measure_operations')
        self.assertEqual(rows[0]['tableName'], 'Ventas')
        self.assertEqual(self.client.calls[-1][1]['request']['filter']['maxResults'], 100000)

    def test_measure_detail_and_filter(self):
        with patch('builtins.input', side_effect=['/Ventas', '1', '']):
            c.simple_list(self.client, 'measure_operations', 'MEDIDAS')
        self.assertIn('expression:\n1', self.output.getvalue())
        self.assertIn('Actual', self.output.getvalue())

    def test_relationship_no_prompt_or_invented_cardinality(self):
        with patch('builtins.input', side_effect=AssertionError('No debe preguntar')):
            c.simple_list(self.client, 'relationship_operations', 'RELACIONES')
        self.assertIn('A[Id]', self.output.getvalue())
        self.assertNotIn('fromCardinality', self.output.getvalue())

    def test_nested_response_and_numeric_name(self):
        result = {'content': [{'type': 'text', 'text': json.dumps({'data': json.dumps([{'name': '123'}])})}]}
        rows = c.object_rows(c.unwrap_result(result))
        self.assertEqual(rows, [{'name': '123'}])

    def test_structured_content(self):
        self.assertEqual(c.unwrap_result({'structuredContent': {'data': []}, 'content': []}), {'data': []})

    def test_embedded_resource(self):
        result = {'content': [{'type': 'resource', 'resource': {'text': '{"rows":[{"x":1}]}'}}]}
        self.assertEqual(c.dax_rows(c.unwrap_result(result)), [{'x': 1}])

    def test_error_variants(self):
        for response in ({'isError': True, 'content': [{'type': 'text', 'text': '{"message":"DAX invalido"}'}]},
                         {'data': {'success': False, 'message': 'DAX invalido'}},
                         {'status': 'Failed', 'errorMessage': 'DAX invalido'}):
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, 'DAX invalido'):
                c.unwrap_result(response)

    def test_read_only_all_entry_points(self):
        for op in ('Create', 'Update', 'Delete'):
            with self.subTest(op=op), self.assertRaises(RuntimeError):
                c.request_call(self.client, 'measure_operations', op)
        with self.assertRaises(RuntimeError):
            c.enforce_policy(self.client, 'unknown_tool', {'request': {'operation': 'Execute'}})
        self.assertEqual(self.client.calls, [])

    def test_missing_operation_is_clear(self):
        self.client.tools['measure_operations']['inputSchema']['properties']['request']['properties']['operation']['description'] = 'Help, Get, List'
        with self.assertRaisesRegex(RuntimeError, 'no disponible'):
            c.request_call(self.client, 'measure_operations', 'Update')

    def test_schema_ref_and_nullable(self):
        root = {'$defs': {'r': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}}}
        schema = {'anyOf': [{'$ref': '#/$defs/r'}, {'type': 'null'}]}
        self.assertEqual(c.resolve_schema(root, schema)['required'], ['name'])
        with self.assertRaises(ValueError):
            c.validate_schema(root, schema, {})

    def test_real_schema_request_construction(self):
        self.client.writable = True
        obj = {'tableName': 'Ventas', 'name': 'Nueva', 'expression': 'SUM(Ventas[Importe])'}
        fields = c.build_object_fields(self.client, 'measure_operations', 'Create', obj)
        c.request_call(self.client, 'measure_operations', 'Create', **fields)
        req = self.client.calls[-1][1]['request']
        self.assertEqual(req['definitions'], [obj])
        self.assertEqual(req['connectionName'], 'conexion-prueba')

    def test_delete_requires_exact_confirmation(self):
        self.client.writable = True
        with patch('builtins.input', return_value='s'):
            result = c.apply_objects(self.client, 'measure_operations', 'Delete', [{'name': 'Total', 'tableName': 'Ventas'}])
        self.assertEqual(result, [])
        self.assertEqual(self.client.calls, [])

    def test_multi_delete_only_selected_no_cascade(self):
        self.client.writable = True
        selected = [{'name': x, 'tableName': 'Ventas'} for x in ('Uno', 'Tres')]
        with patch('builtins.input', return_value='CONFIRMAR'):
            outcomes = c.apply_objects(self.client, 'measure_operations', 'Delete', selected)
        reqs = [args['request'] for _, args in self.client.calls if args['request']['operation'] == 'Delete']
        self.assertEqual([r['references'][0]['name'] for r in reqs], ['Uno', 'Tres'])
        self.assertTrue(all(r['shouldCascadeDelete'] is False for r in reqs))
        self.assertEqual(len(outcomes), 2)

    def test_partial_failure_continues(self):
        self.client.writable = True
        def write(args):
            if args['request']['definitions'][0]['name'] == 'Mala':
                return {'success': False, 'message': 'Expresion invalida'}
            return {'success': True}
        self.client.responses['measure_operations', 'Create'] = write
        objects = [{'name': x, 'tableName': 'Ventas', 'expression': '1'} for x in ('Mala', 'Buena')]
        with patch('builtins.input', return_value='CONFIRMAR'):
            result = c.apply_objects(self.client, 'measure_operations', 'Create', objects)
        self.assertEqual([state for _, state in result], ['error', 'confirmado'])

    def test_timeout_stops_batch_no_retry(self):
        self.client.writable = True
        self.client.responses['measure_operations', 'Create'] = TimeoutError('Resultado incierto')
        objects = [{'name': x, 'tableName': 'Ventas', 'expression': '1'} for x in ('Uno', 'Dos')]
        with patch('builtins.input', return_value='CONFIRMAR'):
            result = c.apply_objects(self.client, 'measure_operations', 'Create', objects)
        self.assertEqual(len(result), 1)
        self.assertIsNone(self.client.connection_name)

    def test_unconfirmed_response_not_success(self):
        self.assertFalse(c.confirmed_success({'message': 'Awaiting consent'}))
        self.assertTrue(c.confirmed_success({'data': {'results': [{'success': True}]}}))

    def test_empty_write_response_verified_by_read(self):
        self.client.writable = True
        self.client.responses['measure_operations', 'Update'] = {'content': [], 'isError': False}
        with patch('builtins.input', return_value='CONFIRMAR'):
            outcomes = c.apply_objects(self.client, 'measure_operations', 'Update',
                                       [{'tableName': 'Ventas', 'name': 'Total', 'expression': '1'}])
        self.assertEqual(outcomes[0][1], 'confirmado')
        self.assertIn('CONFIRMADO POR LECTURA', self.output.getvalue())

    def test_empty_response_wrong_state_never_confirmed(self):
        self.client.writable = True
        self.client.responses['measure_operations', 'Update'] = {'content': [], 'isError': False}
        with patch('builtins.input', return_value='CONFIRMAR'):
            outcomes = c.apply_objects(self.client, 'measure_operations', 'Update',
                                       [{'tableName': 'Ventas', 'name': 'Total', 'expression': '99'}])
        self.assertEqual(outcomes[0][1], 'incierto')

    def test_duplicate_measure_blocked(self):
        self.client.writable = True
        with patch('builtins.input', side_effect=['1', 'Total']), self.assertRaisesRegex(ValueError, 'ya existe'):
            c.edit_objects(self.client, 'measure_operations', 'Create', 'MEDIDAS')
        self.assertFalse(any(args['request']['operation'] == 'Create' for _, args in self.client.calls))

    def test_create_multiple_ui(self):
        self.client.writable = True
        with patch('builtins.input', side_effect=['1', 'Uno', 'SUM(', 'Ventas[Importe])', 'FIN', 's',
                                                '1', 'Dos', '2', 'FIN', 'n', 'CONFIRMAR']):
            c.edit_objects(self.client, 'measure_operations', 'Create', 'MEDIDAS')
        reqs = [args['request'] for _, args in self.client.calls if args['request']['operation'] == 'Create']
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0]['definitions'][0]['expression'], 'SUM(\nVentas[Importe])')

    def test_update_only_selected_and_show_diff(self):
        self.client.writable = True
        with patch('builtins.input', side_effect=['1', '2', 'FIN', '', '', '', 'CONFIRMAR']):
            c.edit_objects(self.client, 'measure_operations', 'Update', 'MEDIDAS')
        updates = [args['request'] for _, args in self.client.calls if args['request']['operation'] == 'Update']
        self.assertEqual(updates[0]['definitions'], [{'name': 'Total', 'tableName': 'Ventas', 'expression': '2'}])
        self.assertIn('-1', self.output.getvalue())
        self.assertIn('+2', self.output.getvalue())

    def test_column_type_guard(self):
        self.assertTrue(c.calculated_column({'columnType': 'Calculated', 'expression': '1'}))
        self.assertTrue(c.calculated_column({'isCalculated': True, 'expression': '1'}))
        for obj in ({'columnType': 'Data'}, {'columnType': 'CalculatedTableColumn', 'expression': '1'}, {'expression': '1'}):
            self.assertFalse(c.calculated_column(obj))

    def test_column_create_update_delete_requests(self):
        self.client.writable = True
        obj = {'name': 'Calculada', 'tableName': 'Ventas', 'expression': '1'}
        for operation in ('Create', 'Update', 'Delete'):
            with self.subTest(operation=operation), patch('builtins.input', return_value='CONFIRMAR'):
                outcomes = c.apply_objects(self.client, 'column_operations', operation, [obj])
                self.assertEqual(outcomes[0][1], 'confirmado')
                req = self.client.calls[-1][1]['request']
                key = 'references' if operation == 'Delete' else 'definitions'
                self.assertEqual(req[key][0]['tableName'], 'Ventas')

    def test_empty_name_and_unsupported_property(self):
        self.client.writable = True
        with patch('builtins.input', side_effect=['1', '']), self.assertRaises(ValueError):
            c.edit_objects(self.client, 'measure_operations', 'Create', 'MEDIDAS')
        with self.assertRaises(RuntimeError):
            c.build_object_fields(self.client, 'measure_operations', 'Update', {'name': 'x', 'inventada': 'x'})

    def test_missing_tool(self):
        del self.client.tools['measure_operations']
        with self.assertRaisesRegex(RuntimeError, 'no disponible'):
            c.list_objects(self.client, 'measure_operations')

    def test_imported_column_not_modified(self):
        self.client.writable = True
        self.client.responses['column_operations', 'Get'] = {'data': [{'name': 'Total', 'tableName': 'Ventas', 'columnType': 'Data'}]}
        with patch('builtins.input', side_effect=['1', '1']):
            c.edit_objects(self.client, 'column_operations', 'Update', 'COLUMNAS')
        self.assertFalse(any(args['request']['operation'] == 'Update' for _, args in self.client.calls))

    def test_choose_multiple_deduplicates(self):
        with patch('builtins.input', return_value='1,3,1'):
            self.assertEqual(c.choose(['a', 'b', 'c'], multiple=True), ['a', 'c'])

    def test_dax_inline_execute_only(self):
        self.client.responses['dax_query_operations', 'Execute'] = {'data': {'rows': [{'x': 1}]}}
        with patch('builtins.input', side_effect=['EVALUATE', 'ROW("x",1)', 'FIN', '']):
            c.dax_query(self.client)
        req = self.client.calls[-1][1]['request']
        self.assertEqual(req['resultMode'], 'Inline')
        self.assertEqual(req['operation'], 'Execute')

    def test_csv_values_and_overwrite_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'result.csv'
            c.save_csv([{'x': 'á,\nlargo', 'y': 1}], path)
            import csv
            with path.open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual(list(csv.DictReader(stream))[0]['x'], 'á,\nlargo')
            with self.assertRaises(ValueError):
                c.save_csv([], path)


class TransportTests(unittest.TestCase):
    def make_transport(self):
        client = c.MCP.__new__(c.MCP)
        client.inbox = queue.Queue()
        client.counter = 0
        client.send = Mock()
        return client

    def test_notifications_late_ids_and_server_ping(self):
        client = self.make_transport()
        for response in ({'method': 'notifications/message'}, {'id': 99, 'result': {}},
                         {'method': 'ping', 'id': 20}, {'id': 1, 'result': {'ok': True}}):
            client.inbox.put(response)
        self.assertEqual(client.call('test', {}), {'ok': True})
        self.assertEqual(client.send.call_args.args[0]['id'], 20)

    def test_process_closed(self):
        client = self.make_transport()
        client.inbox.put({'_closed': True})
        with self.assertRaisesRegex(RuntimeError, 'se cerro'):
            client.call('test', {})

    def test_json_rpc_error(self):
        client = self.make_transport()
        client.inbox.put({'id': 1, 'error': {'code': -32602, 'message': 'Bad params'}})
        with self.assertRaisesRegex(RuntimeError, 'Bad params'):
            client.call('test', {})

    def test_absolute_deadline_with_notifications(self):
        client = self.make_transport()
        client.inbox.put({'method': 'notifications/message'})
        with patch.object(c.time, 'monotonic', side_effect=[0, 0.1, 2]), self.assertRaises(TimeoutError):
            client.call('test', {}, timeout=1)

    def test_consent_declined_by_default(self):
        client = self.make_transport()
        request = {'method': 'elicitation/create', 'id': 4,
                   'params': {'message': 'Permitir edicion?', 'requestedSchema': {'type': 'object', 'properties': {}}}}
        with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value=''):
            client._server_request(request)
        self.assertEqual(client.send.call_args.args[0]['result'], {'action': 'decline'})


class TransferTests(unittest.TestCase):
    def test_roundtrip_byte_for_byte(self):
        samples = [b'', b'\n', b'\r\n', b'a', b'a\n', b'a\r\n', b'    \n  x\r\n\tpass',
                   '\ufeff# español\r\n0|literal\n    print("á")'.encode('utf-8'), b'a\rb\n', b'a\x0bb']
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(decode_bytes(encode_bytes(sample)), sample)

    def test_actual_client_roundtrip(self):
        original = Path(c.__file__).read_bytes()
        self.assertEqual(decode_bytes(encode_bytes(original)), original)

    def test_expected_format(self):
        self.assertEqual(encode_bytes(b'class MCP:\n    pass\n'), b'0|class MCP:\n4|pass\n')

    def test_malformed_input(self):
        for data in (b'pass\n', b'-1|x', b'1000001|x', b'|x'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_bytes(data)

    def test_files_and_no_accidental_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            source, encoded, restored = (Path(temp) / name for name in ('original.py', 'codigo.txt', 'restored.py'))
            source.write_bytes(b'# test\r\n    x=1')
            encode_file(source, encoded)
            decode_file(encoded, restored)
            self.assertEqual(source.read_bytes(), restored.read_bytes())
            with self.assertRaises(FileExistsError):
                decode_file(encoded, restored)


if __name__ == '__main__':
    unittest.main()
