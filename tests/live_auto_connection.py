"""Pruebas reales de --write sin preguntas, reconexion y transporte MCP perdido."""
import argparse
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def run(model):
    client, reader = c.MCP(writable=True), c.MCP(writable=False)
    prefix = '__AUTO_CONN_' + uuid.uuid4().hex[:10] + '_'
    table = None
    report = {'tests': [], 'clean': False}
    original_tool = client.tool
    try:
        instances = c.list_instances(client)
        choices = [i for i, row in enumerate(instances, 1) if row.get('parentWindowTitle') == model]
        if len(choices) != 1:
            raise RuntimeError('Debe existir exactamente un modelo con ese titulo')
        for active in (client, reader):
            with patch('builtins.input', return_value=str(choices[0])):
                c.connect(active)
        def refresh_reader():
            nonlocal reader
            reader.close()
            reader = c.MCP(writable=False)
            with patch('builtins.input', return_value=str(choices[0])):
                c.connect(reader)
        identity = json.loads(json.dumps(client.target_identity))
        tables = c.list_objects(client, 'table_operations')
        table = min([t for t in tables if t.get('columnCount', 0) > 0 and not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_'))], key=lambda t: t['columnCount'])['name']
        def op(action, name, expression=None):
            item = dict(action=action, table=table, name=name)
            if expression is not None:
                item['expression'] = expression
            return item
        def block(label, operations, expected_failure=False):
            # StopIteration makes any extra prompt after FIN fail this real test.
            with patch('builtins.input', side_effect=[json.dumps({'operations': operations}), 'FIN']) as inp:
                results = c.ai_block(client)
            assert inp.call_count == 2, 'Se solicito una interaccion adicional'
            assert (client.last_block_status == 'fallido') == expected_failure
            assert c.same_target(identity, client.target_identity)
            if expected_failure:
                assert client.last_recovery == 'verificada'
            else:
                assert all(r['status'] == 'correcta' for r in results)
            report['tests'].append({'name': label, 'operations': len(c.parse_ai_block(json.dumps({'operations': operations}))),
                                    'inputs': 'JSON y FIN, sin confirmaciones', 'passed': True,
                                    'recovery': client.last_recovery})
            print('PRUEBA SUPERADA:', label, flush=True)

        names = [prefix + str(i) for i in range(10)]
        operations = [op('create_measure', name, str(i)) for i, name in enumerate(names)]
        operations += [op('update_measure', name, str(i + 100)) for i, name in enumerate(names[:5])]
        operations += [op('delete_measure', name) for name in names[5:]]
        block('20_operaciones_sin_confirmacion', operations)
        actual_list = c.list_instances
        def changed_presentation(active):
            rows = actual_list(active)
            return [dict(row, port=str(row['port']), processId=str(row.get('processId')),
                         startTime=c.normalized_start(row.get('startTime')), parentWindowTitle=row.get('parentWindowTitle', '') + ' *') for row in rows]
        with patch.object(c, 'list_instances', side_effect=changed_presentation):
            assert c.ensure_connected(client)
            block('titulo_y_formatos_no_cambian_modelo', [op('update_measure', names[0], '321')])
        # Real disconnection of the named connection inside the existing MCP.
        original_tool('connection_operations', {'request': {'operation': 'Disconnect', 'connectionName': client.connection_name}})
        count = getattr(client, 'reconnection_count', 0)
        block('desconexion_real_y_reconexion', [op('create_measure', prefix + 'RECONNECT', '1'), op('delete_measure', prefix + 'RECONNECT')])
        assert client.reconnection_count > count
        old_session = client.mcp_session_token
        client.process.kill()
        client.process.wait(timeout=5)
        block('reinicio_MCP_antes_del_bloque', [op('update_measure', names[1], '222')])
        assert client.mcp_session_token != old_session
        for mode in ('respuesta_perdida', 'proceso_perdido_compensacion', 'proceso_perdido_transaccion'):
            name = prefix + mode
            create_calls = [0]
            old_session = client.mcp_session_token
            original_tool = client.tool
            def lose_response(tool, args):
                value = original_tool(tool, args)
                req = args['request']
                definitions = req.get('definitions') or []
                if tool == 'measure_operations' and req['operation'] == 'Create' and definitions and definitions[0].get('name') == name:
                    create_calls[0] += 1
                    if mode != 'respuesta_perdida':
                        client.process.kill()
                        client.process.wait(timeout=5)
                        raise BrokenPipeError('Proceso MCP terminado despues de enviar la escritura')
                    raise TimeoutError('Respuesta perdida despues de aplicar la escritura real')
                return value
            operations = [op('create_measure', name, '3')]
            if mode != 'proceso_perdido_transaccion':
                operations.append({'action': 'execute_dax', 'query': 'EVALUATE ROW("Valor", 1)'})
            with patch.object(client, 'tool', side_effect=lose_response):
                block(mode, operations, expected_failure=True)
            assert create_calls[0] == 1, 'Se repitio una escritura incierta'
            refresh_reader()
            assert c.target_state(reader, 'measure_operations', {'tableName': table, 'name': name}) is None
            if mode != 'respuesta_perdida':
                assert client.mcp_session_token != old_session
        block('error_DAX_y_recuperacion', [op('update_measure', names[2], '999'),
               {'action': 'execute_dax', 'query': 'EVALUATE __ERROR_AUTOMATICO_PRUEBA__'}], expected_failure=True)
        refresh_reader()
        assert c.target_state(reader, 'measure_operations', {'tableName': table, 'name': names[2]})['expression'].strip() == '102'
        # Wrong-instance data is injected; no other real model is written.
        with patch.object(c, 'list_instances', return_value=[dict(identity['instance'], port=int(identity['instance']['port']) + 1)]):
            assert not c.ensure_connected(client)
        assert c.ensure_connected(client)
        report['tests'].append({'name': 'otra_instancia_rechazada_datos_inyectados', 'passed': True, 'writesToOtherModel': 0})
    finally:
        try:
            if table and c.ensure_connected(client):
                remaining = [r['name'] for r in c.list_objects(client, 'measure_operations') if r['name'].startswith(prefix)]
                if remaining:
                    block('limpieza', [{'action': 'delete_measures', 'table': table, 'names': remaining}])
                refresh_reader()
                report['clean'] = not any(r['name'].startswith(prefix) for r in c.list_objects(reader, 'measure_operations'))
        finally:
            reader.close()
            client.close()
            Path('resultado_automatico_conexion.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert report['clean']
    print('PRUEBAS AUTOMATICAS REALES COMPLETADAS:', json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo', required=True)
    parser.add_argument('--confirmar-escrituras', required=True, action='store_true', help='Autoriza los ensayos destructivos del script de pruebas')
    args = parser.parse_args()
    run(args.modelo)
