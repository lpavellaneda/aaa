import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cliente as c
from tests.test_ai_block import ModelClient, measure
from tests.test_cliente import INSTANCE


class IdentifiedClient(ModelClient):
    def __init__(self):
        super().__init__()
        self.live_instance = dict(INSTANCE, processId=2208, startTime='2026-11-21T08:55:02.411783-05:00')
        self.instance = dict(self.live_instance)
        self.target_identity = None
        self.database = 'database-original'
        self.aliases = {self.connection_name: self.database}
        self.alias_counter = 0
        self.session = 'engine-session-1'
        self.mcp_session_token = 'mcp-1'
        self.active_transaction = None
        self.retired_mcp_sessions = []
        self.debug = False
        self.close = Mock()

    def tool(self, name, args):
        if name != 'connection_operations':
            return super().tool(name, args)
        self.calls.append((name, copy.deepcopy(args)))
        req = args['request']
        if req['operation'] == 'ListLocalInstances':
            return {'data': [dict(self.live_instance)]}
        if req['operation'] == 'Connect':
            self.alias_counter += 1
            alias = 'conexion-nueva-' + str(self.alias_counter)
            self.aliases[alias] = self.database
            return {'data': alias}
        if req['operation'] == 'GetConnection':
            alias = req['connectionName']
            if alias not in self.aliases:
                raise RuntimeError(f"Connection '{alias}' not found")
            return {'data': {'connectionName': alias, 'databaseName': self.aliases[alias],
                             'serverName': f'localhost:{self.live_instance["port"]}', 'sessionId': self.session}}
        raise RuntimeError('Unsupported connection action')


class AutoConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = IdentifiedClient()
        self.client.backup_dir = Path(self.temp.name)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.assertTrue(c.ensure_connected(self.client))

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temp.cleanup()

    def plan(self, ops):
        return c.validate_ai_block(self.client, c.parse_ai_block(json.dumps({'operations': ops})))[0]

    def writes(self):
        return [args['request'] for tool, args in self.client.calls if args['request']['operation'] in ('Create', 'Update', 'Delete')]

    def test_display_title_changes_do_not_change_identity(self):
        self.client.live_instance['parentWindowTitle'] = 'Prueba * - otra pagina'
        self.assertTrue(c.ensure_connected(self.client))
        self.assertEqual(self.client.alias_counter, 0)

    def test_port_pid_and_equivalent_time_formats(self):
        self.client.live_instance.update(port=str(INSTANCE['port']), processId='2208', startTime='2026-11-21T13:55:02.411783Z')
        self.assertTrue(c.ensure_connected(self.client))

    def test_missing_optional_timestamp_does_not_false_positive(self):
        del self.client.live_instance['startTime']
        self.assertTrue(c.ensure_connected(self.client))

    def test_same_port_different_process_is_rejected(self):
        self.client.live_instance['processId'] = 9999
        self.assertFalse(c.ensure_connected(self.client))
        self.assertEqual(self.client.alias_counter, 0)
        self.assertIn('puerto y proceso', self.client.connection_issue)

    def test_same_title_different_port_is_not_followed(self):
        self.client.live_instance['port'] += 1
        self.assertFalse(c.ensure_connected(self.client))
        self.assertEqual(self.client.alias_counter, 0)

    def test_different_database_is_rejected(self):
        self.client.aliases[self.client.connection_name] = 'database-otra'
        self.assertFalse(c.ensure_connected(self.client))
        self.assertIn('base de datos', self.client.connection_issue)
        self.assertEqual(self.client.alias_counter, 0)

    def test_lost_alias_reconnects_same_target_with_new_alias(self):
        old = self.client.connection_name
        del self.client.aliases[old]
        self.assertTrue(c.ensure_connected(self.client))
        self.assertNotEqual(old, self.client.connection_name)
        self.assertEqual(self.client.target_identity['database'], 'database-original')

    def test_reconnection_to_wrong_database_is_blocked(self):
        self.client.aliases.clear()
        self.client.database = 'database-otra'
        self.assertFalse(c.ensure_connected(self.client))
        self.assertIsNone(self.client.connection_name)

    def test_dax_message_containing_connection_does_not_clear_state(self):
        alias = self.client.connection_name
        c.invalidate_on_error(self.client, RuntimeError("DAX name 'connection' cannot be determined"))
        self.assertEqual(self.client.connection_name, alias)

    def test_invalidation_preserves_selected_target(self):
        c.invalidate_on_error(self.client, TimeoutError('no response'))
        self.assertIsNone(self.client.connection_name)
        self.assertIsNotNone(self.client.target_identity)
        self.assertTrue(c.ensure_connected(self.client))

    def test_lost_alias_before_batch_does_not_stop_valid_batch(self):
        plan = self.plan([measure()])
        self.client.aliases.clear()
        result = c.execute_ai_plan(self.client, plan)
        self.assertEqual(result[0]['status'], 'correcta')
        self.assertEqual(sum(req['operation'] == 'Create' for req in self.writes()), 1)

    def test_pre_send_connection_failure_does_not_claim_incomplete_undo(self):
        plan = self.plan([measure()])
        with patch.object(c, 'ensure_connected', side_effect=[True, False]):
            c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.client.last_recovery, 'no_necesaria')
        self.assertIn('No se envio ninguna escritura', self.output.getvalue())

    def test_pending_transaction_blocks_new_batch(self):
        plan = self.plan([measure()])
        self.client.active_transaction = 'transaccion-anterior'
        with self.assertRaisesRegex(RuntimeError, 'pendiente de cierre'):
            c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.writes(), [])

    def test_transaction_does_not_reconnect_into_another_engine_session(self):
        self.client.active_transaction = 'tx-original'
        self.client.aliases.clear()
        self.assertFalse(c.ensure_connected(self.client, allow_reconnect=False, allow_restart=False))
        self.assertEqual(self.client.alias_counter, 0)

    def test_transaction_rejects_engine_session_change(self):
        self.client.active_transaction = 'tx-original'
        self.client.session = 'engine-session-2'
        self.assertFalse(c.ensure_connected(self.client, allow_reconnect=False))
        self.assertEqual(self.client.alias_counter, 0)

    def test_timeout_after_applied_write_is_not_retried_and_is_recovered(self):
        plan = self.plan([measure()])
        original = self.client.tool
        def lost_response(tool, args):
            value = original(tool, args)
            if tool == 'measure_operations' and args['request']['operation'] == 'Create':
                raise TimeoutError('Respuesta de escritura perdida')
            return value
        with patch.object(self.client, 'tool', side_effect=lost_response):
            c.execute_ai_plan(self.client, plan)
        self.assertEqual(self.client.last_block_status, 'fallido')
        self.assertEqual(self.client.last_recovery, 'verificada')
        self.assertEqual(sum(req['operation'] == 'Create' for req in self.writes()), 1)
        self.assertNotIn(('Ventas', 'Nueva'), self.client.catalog['measure_operations'])

    def test_main_write_flow_has_only_model_menu_and_json_inputs(self):
        block = json.dumps(measure())
        with patch.object(c, 'MCP', return_value=self.client), \
             patch.object(c.sys, 'argv', ['cliente.py', '--write', '--backup-dir', self.temp.name]), \
             patch('builtins.input', side_effect=['1', '13', block, 'FIN', '0']) as inp:
            c.main()
        self.assertEqual(inp.call_count, 5)
        self.assertNotIn('Escribe EDITAR', self.output.getvalue())
        self.assertNotIn('Escribe CONFIRMAR', self.output.getvalue())
        self.assertEqual(self.client.last_block_status, 'completado')

    def test_invalid_block_never_asks_confirmation_or_writes(self):
        with patch('builtins.input', side_effect=['{', 'FIN']) as inp, self.assertRaises(ValueError):
            c.ai_block(self.client)
        self.assertEqual(inp.call_count, 2)
        self.assertEqual(self.writes(), [])

    def test_readonly_write_block_is_still_disabled(self):
        self.client.writable = False
        with patch('builtins.input', side_effect=[json.dumps(measure()), 'FIN']), self.assertRaises(ValueError):
            c.ai_block(self.client)
        self.assertEqual(self.writes(), [])

    def test_boolean_server_consent_is_covered_by_write_without_input(self):
        client = c.MCP.__new__(c.MCP)
        client.writable, client._auto_write_scope, client.send = True, True, Mock()
        message = {'id': 'consent', 'method': 'elicitation/create', 'params': {
            'message': 'Allow changes to this semantic model?',
            'requestedSchema': {'type': 'object', 'properties': {'allowChanges': {'type': 'boolean'}}, 'required': ['allowChanges']}}}
        with patch('builtins.input', side_effect=AssertionError('No debe pedir confirmacion')):
            client._server_request(message)
        self.assertEqual(client.send.call_args.args[0]['result'], {'action': 'accept', 'content': {'allowChanges': True}})

    def test_unknown_server_request_is_declined_without_interaction(self):
        client = c.MCP.__new__(c.MCP)
        client.writable, client._auto_write_scope, client.send = True, True, Mock()
        message = {'id': 1, 'method': 'elicitation/create', 'params': {'message': 'Provide custom information',
                   'requestedSchema': {'type': 'object', 'properties': {'extra': {'type': 'string'}}}}}
        with patch('builtins.input', side_effect=AssertionError('No debe preguntar')):
            client._server_request(message)
        self.assertEqual(client.send.call_args.args[0]['result']['action'], 'decline')


if __name__ == '__main__':
    unittest.main()
