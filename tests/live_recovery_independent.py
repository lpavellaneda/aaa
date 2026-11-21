"""Verifica recuperaciones desde otro proceso MCP y mediante evaluacion DAX."""
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
    report = {'independentRecoveryVerified': False, 'cases': []}
    prefix = '__REC_EXT_' + uuid.uuid4().hex[:10]
    table = None
    names = [prefix + suffix for suffix in ('_A', '_B', '_NEW')]
    try:
        instances = c.list_instances(client)
        choices = [i for i, row in enumerate(instances, 1) if row.get('parentWindowTitle') == model]
        assert len(choices) == 1
        for active in (client, reader):
            with patch('builtins.input', return_value=str(choices[0])):
                c.connect(active)
        tables = c.list_objects(client, 'table_operations')
        table = min([t for t in tables if t.get('columnCount', 0) > 0 and not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_'))], key=lambda t: t['columnCount'])['name']
        def op(action, name, expression=None):
            item = dict(action=action, table=table, name=name)
            if expression is not None:
                item['expression'] = expression
            return item
        def execute(ops):
            plan, _ = c.validate_ai_block(client, ops)
            return c.execute_ai_plan(client, plan)
        execute([op('create_measure', names[0], '1'), op('create_measure', names[1], '2')])
        references = [{'tableName': table, 'name': name} for name in names[:2]]
        expected = [c.target_state(reader, 'measure_operations', ref) for ref in references]
        for mode in ('rollback_servidor', 'compensacion', 'error_semantico'):
            ops = [op('update_measure', names[0], '999'), op('delete_measure', names[1]), op('create_measure', names[2], '3')]
            original = c.request_call
            if mode == 'compensacion':
                ops.append({'action': 'execute_dax', 'query': 'EVALUATE __ERROR_MCP_PRUEBA__'})
                execute(ops)
            elif mode == 'error_semantico':
                ops[-1]['expression'] = 'SUM('
                execute(ops)
            else:
                def inject(active, tool, operation, **fields):
                    if tool == 'measure_operations' and operation == 'Create' and fields.get('definitions', [{}])[0].get('name') == names[2]:
                        return original(active, 'dax_query_operations', 'Execute', query='EVALUATE __ERROR_MCP_PRUEBA__', resultMode='Inline')
                    return original(active, tool, operation, **fields)
                with patch.object(c, 'request_call', side_effect=inject):
                    execute(ops)
            assert client.last_block_status == 'fallido' and client.last_recovery == 'verificada'
            # Reader was connected before changes, so reconnect its same model to
            # discard any prior metadata cache before independently verifying.
            with patch('builtins.input', return_value=str(choices[0])):
                c.connect(reader)
            for ref, before in zip(references, expected):
                assert c.stable_state(c.target_state(reader, 'measure_operations', ref)) == c.stable_state(before)
            assert c.target_state(reader, 'measure_operations', {'tableName': table, 'name': names[2]}) is None
            values = c.dax_rows(c.request_call(reader, 'dax_query_operations', 'Execute',
                    query=f'EVALUATE ROW("A", [{names[0]}], "B", [{names[1]}])', resultMode='Inline'))
            assert list(values[0].values()) == [1, 2]
            report['cases'].append({'mode': mode, 'metadataRestored': True, 'evaluatedValues': [1, 2], 'passed': True})
        report['independentRecoveryVerified'] = True
    finally:
        try:
            if table:
                present = {r['name'] for r in c.list_objects(client, 'measure_operations')}
                ops = [{'action': 'delete_measure', 'table': table, 'name': name} for name in names if name in present]
                if ops:
                    plan, _ = c.validate_ai_block(client, ops)
                    c.execute_ai_plan(client, plan)
        finally:
            reader.close()
            client.close()
            Path('resultado_recuperacion_independiente.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('RECUPERACIONES VERIFICADAS DESDE OTRO PROCESO MCP:', json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo', required=True)
    parser.add_argument('--confirmar-escrituras', required=True, action='store_true')
    args = parser.parse_args()
    run(args.modelo)
