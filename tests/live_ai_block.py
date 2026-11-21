"""Prueba real opcional de opcion 13, con nombres temporales y limpieza."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def run(model):
    client = c.MCP(writable=True)
    prefix = '__PRUEBA_BLOQUE_' + uuid.uuid4().hex[:12] + '_'
    created = []
    report = {'verified': False, 'temporaryObjectsRemoved': False}
    try:
        instances = c.list_instances(client)
        indices = [i for i, item in enumerate(instances, 1) if item.get('parentWindowTitle') == model]
        if len(indices) != 1:
            raise RuntimeError('No se encontro exactamente un modelo con ese titulo')
        with patch('builtins.input', return_value=str(indices[0])):
            c.connect(client)
        tables = c.list_objects(client, 'table_operations')
        candidates = [t for t in tables if t.get('columnCount', 0) > 0 and not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_'))]
        table = min(candidates, key=lambda t: t.get('columnCount', 1000000))['name']
        tools = ('measure_operations', 'column_operations')
        def snapshot():
            return {tool: {(r['tableName'], r['name']) for r in c.list_objects(client, tool)} for tool in tools}
        before = snapshot()
        m1, m2, col1, col2 = [prefix + suffix for suffix in ('M1', 'M2', 'C1', 'C2')]
        def obj(action, name, expression=None):
            result = {'action': action, 'table': table, 'name': name}
            if expression is not None:
                result['expression'] = expression
            return result
        operations = [obj('create_measure', m1, '1'), obj('create_measure', m2, f'[{m1}] + 1'),
                      dict(obj('update_measure', m1, '10'), description='Prueba temporal opcion 13'),
                      obj('create_column', col1, '1'), obj('create_column', col2, '2'),
                      obj('update_column', col1, '3'),
                      {'action': 'execute_dax', 'query': f'EVALUATE ROW("Base", [{m1}], "Dependiente", [{m2}])'},
                      {'action': 'delete_measures', 'table': table, 'names': [m2, m1]},
                      obj('delete_column', col2), obj('delete_column', col1)]
        created = [(tool, {'tableName': table, 'name': name}) for tool, names in
                   ((tools[0], (m1, m2)), (tools[1], (col1, col2))) for name in names]
        lines = [json.dumps({'operations': operations}), 'FIN']
        with patch('builtins.input', side_effect=lines):
            results = c.ai_block(client)
        if len(results) != 11 or any(r['status'] != 'correcta' for r in results):
            raise AssertionError('El bloque no completo todas sus operaciones')
        if snapshot() != before:
            raise AssertionError('El inventario final no coincide con el inicial')
        report.update(verified=True, operations=11, writes=10, queries=1,
                      confirmation='ninguna (--write)', verification='List/Get despues de cada escritura',
                      temporaryObjectsRemoved=True)
    finally:
        try:
            for tool, obj in reversed(created):
                rows = c.list_objects(client, tool)
                if any(r['tableName'] == obj['tableName'] and r['name'] == obj['name'] for r in rows):
                    with patch('builtins.input', return_value='CONFIRMAR'):
                        c.apply_objects(client, tool, 'Delete', [obj])
            if created:
                remaining = [r for tool in tools for r in c.list_objects(client, tool) if r['name'].startswith(prefix)]
                report['temporaryObjectsRemoved'] = not remaining
                if remaining:
                    raise RuntimeError('Quedan objetos temporales; revisa el modelo')
        finally:
            client.close()
            report_path = Path(__file__).resolve().parents[1] / 'resultado_bloque_ia.json'
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('BLOQUE REAL VALIDADO:', json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo', required=True)
    parser.add_argument('--confirmar-escrituras', required=True, action='store_true')
    args = parser.parse_args()
    run(args.modelo)
