"""Pruebas reales de bloques extensos y recuperacion. Modifica objetos de prueba."""
import argparse
import copy
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def run(model, backup_dir):
    client = c.MCP(writable=True)
    client.backup_dir = backup_dir
    prefix = '__TEST_REC_' + uuid.uuid4().hex[:10] + '_'
    tracked, report = [], {'tests': [], 'cleanupVerified': False}
    table = None

    def instruction(action, name, expression=None, **props):
        op = dict(action=action, table=table, name=name, **props)
        if expression is not None:
            op['expression'] = expression
        return op

    def execute(label, ops, expect_failure=False):
        parsed = c.parse_ai_block(json.dumps({'operations': ops}))
        plan, _ = c.validate_ai_block(client, parsed)
        deps = c.dependency_report(client)
        c.check_engine_dependencies(plan, deps)
        result = c.execute_ai_plan(client, plan)
        failed = client.last_block_status == 'fallido'
        assert failed == expect_failure, (label, result)
        if expect_failure:
            assert client.last_recovery == 'verificada', client.last_recovery
        else:
            assert len(result) == len(plan)
        report['tests'].append({'name': label, 'operations': len(plan), 'executed': len(result),
                                'expectedFailure': expect_failure, 'recovery': client.last_recovery,
                                'engineDependencies': deps['engine'], 'passed': True})
        print('PRUEBA SUPERADA:', label, flush=True)
        return result

    try:
        instances = c.list_instances(client)
        indices = [i for i, instance in enumerate(instances, 1) if instance.get('parentWindowTitle') == model]
        if len(indices) != 1:
            raise RuntimeError('Debe haber un unico modelo con el titulo indicado')
        with patch('builtins.input', return_value=str(indices[0])):
            c.connect(client)
        tables = c.list_objects(client, 'table_operations')
        candidates = [t for t in tables if t.get('columnCount', 0) > 0 and not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_'))]
        table = min(candidates, key=lambda t: t.get('columnCount', 1000000))['name']
        names = [prefix + str(i) for i in range(50)]
        tracked.extend(names)
        execute('crear_50_medidas', [instruction('create_measure', name, str(i)) for i, name in enumerate(names)])
        execute('modificar_50_medidas', [instruction('update_measure', name, str(i + 100),
                                                   description='Prueba de bloque extenso', formatString='0') for i, name in enumerate(names)])
        execute('eliminar_20_medidas', [{'action': 'delete_measures', 'table': table, 'names': names[:20]}])
        mixed_names = [prefix + 'MIX' + str(i) for i in range(5)]
        tracked.extend(mixed_names)
        mixed = [instruction('create_measure', name, str(i)) for i, name in enumerate(mixed_names)]
        mixed += [instruction('update_measure', name, '200') for name in names[20:25]]
        mixed += [instruction('delete_measure', name) for name in names[30:35]]
        mixed += [{'action': 'execute_dax', 'query': f'EVALUATE ROW("Valor{i}", [{name}])'} for i, name in enumerate(mixed_names)]
        execute('bloque_mixto_20_operaciones', mixed)
        dep_base, dep_child = prefix + 'BASE', prefix + 'DEPENDIENTE'
        tracked.extend((dep_base, dep_child))
        execute('crear_dependencias', [instruction('create_measure', dep_base, '1'), instruction('create_measure', dep_child, f'[{dep_base}] + 1')])
        deps = c.dependency_report(client)
        assert deps['engine'] and any(r.get('[REFERENCED_OBJECT]') == dep_base and r.get('[OBJECT]') == dep_child for r in deps['rows'])
        try:
            c.validate_ai_block(client, [instruction('delete_measure', dep_base)])
        except ValueError:
            report['tests'].append({'name': 'dependencia_impide_eliminar_base', 'passed': True})
        else:
            raise AssertionError('No se detecto la dependencia')
        execute('quitar_dependencia_y_eliminar', [instruction('update_measure', dep_child, '5'), instruction('delete_measure', dep_base)])
        for native in (True, False):
            mode = 'transaccion' if native else 'compensacion'
            a, b = names[25], names[26]
            new, skipped = prefix + mode + 'NEW', prefix + mode + 'SKIP'
            tracked.extend((new, skipped))
            references = [{'name': name, 'tableName': table} for name in (a, b)]
            previous = [c.target_state(client, 'measure_operations', ref) for ref in references]
            tx_tool = client.tools.pop('transaction_operations', None) if not native else None
            try:
                ops = [
                    instruction('update_measure', a, '999'), instruction('delete_measure', b),
                    instruction('create_measure', new, '123'),
                    {'action': 'execute_dax', 'query': 'EVALUATE __ERROR_INTENCIONAL_CLIENTE__'},
                    instruction('create_measure', skipped, '456')]
                if native:
                    bad_native = prefix + 'NATIVE_BAD'
                    tracked.append(bad_native)
                    ops[3] = instruction('create_measure', bad_native, '1')
                    original_request = c.request_call
                    def inject_server_failure(active_client, tool, operation, **fields):
                        definitions = fields.get('definitions') or []
                        if tool == 'measure_operations' and operation == 'Create' and definitions and definitions[0].get('name') == bad_native:
                            return original_request(active_client, 'dax_query_operations', 'Execute',
                                                    query='EVALUATE __ERROR_INTENCIONAL_CLIENTE__', resultMode='Inline')
                        return original_request(active_client, tool, operation, **fields)
                    with patch.object(c, 'request_call', side_effect=inject_server_failure):
                        execute('fallo_intermedio_y_recuperacion_' + mode, ops, expect_failure=True)
                else:
                    execute('fallo_intermedio_y_recuperacion_' + mode, ops, expect_failure=True)
            finally:
                if tx_tool:
                    client.tools['transaction_operations'] = tx_tool
            for ref, expected in zip(references, previous):
                assert c.stable_state(c.target_state(client, 'measure_operations', ref)) == c.stable_state(expected)
            assert c.target_state(client, 'measure_operations', {'name': new, 'tableName': table}) is None
            assert c.target_state(client, 'measure_operations', {'name': skipped, 'tableName': table}) is None
        bad = prefix + 'DAX_INVALIDO'
        tracked.append(bad)
        execute('formula_DAX_invalida', [instruction('update_measure', names[27], '777'),
                                        instruction('create_measure', bad, 'SUM(')], expect_failure=True)
        # Independent server session verifies that successful commits are visible externally.
        other = c.MCP(writable=False)
        try:
            with patch('builtins.input', return_value=str(indices[0])):
                c.connect(other)
            detail = c.target_state(other, 'measure_operations', {'name': names[20], 'tableName': table})
            assert detail['expression'].strip() == '200'
            values = c.dax_rows(c.request_call(other, 'dax_query_operations', 'Execute',
                                             query=f'EVALUATE ROW("Valor", [{names[20]}])', resultMode='Inline'))
            assert list(values[0].values()) == [200]
            for offset in (25, 26, 27):
                recovered = c.target_state(other, 'measure_operations', {'name': names[offset], 'tableName': table})
                assert recovered['expression'].strip() == str(offset + 100)
            report['tests'].append({'name': 'lectura_desde_otro_servidor_MCP', 'passed': True})
        finally:
            other.close()
    finally:
        try:
            if table and client.connection_name:
                remaining = [r['name'] for r in c.list_objects(client, 'measure_operations') if r['name'].startswith(prefix)]
                # Read current dependency order: dependent generated measure first.
                remaining.sort(key=lambda name: name.endswith('DEPENDIENTE'), reverse=True)
                if remaining:
                    execute('limpieza_final', [{'action': 'delete_measures', 'table': table, 'names': remaining}])
                report['cleanupVerified'] = not any(r['name'].startswith(prefix) for r in c.list_objects(client, 'measure_operations'))
        finally:
            client.close()
            Path('resultado_recuperacion.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert report['cleanupVerified']
    print('PRUEBAS REALES COMPLETADAS:', json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo', required=True)
    parser.add_argument('--confirmar-escrituras', required=True, action='store_true')
    parser.add_argument('--backup-dir', type=Path, default=Path.cwd() / 'backups')
    args = parser.parse_args()
    run(args.modelo, args.backup_dir)
