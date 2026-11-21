"""Prueba real de escritura con objetos temporales y limpieza obligatoria."""
import argparse
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def consent(prompt=''):
    # Only used after explicit --confirmar-escrituras, for this temporary-object run.
    print(prompt, end='')
    if 'CONFIRMAR' in prompt:
        print('CONFIRMAR [prueba autorizada]')
        return 'CONFIRMAR'
    if 'Autorizar esta solicitud del servidor?' in prompt or ' *:' in prompt or '(Enter=omitir):' in prompt:
        print('s [prueba autorizada]')
        return 's'
    raise RuntimeError('Solicitud interactiva inesperada; prueba detenida')


def identity(rows):
    return {(r.get('tableName'), r['name']) for r in rows}


def run(model):
    client = c.MCP(writable=True)
    attempted = []
    prefix = '__PRUEBA_CLIENTE_' + uuid.uuid4().hex[:12] + '_'
    report = {'model': model, 'prefix': prefix, 'steps': [], 'cleanup': [], 'verified': False}
    try:
        instances = c.list_instances(client)
        matches = [(i, x) for i, x in enumerate(instances, 1) if x.get('parentWindowTitle') == model]
        if len(matches) != 1:
            raise RuntimeError('Debe existir exactamente un modelo con el titulo indicado')
        with patch('builtins.input', return_value=str(matches[0][0])):
            c.connect(client)
        if not c.ensure_connected(client):
            raise RuntimeError('Conexion invalida')
        tools = ('measure_operations', 'column_operations')
        before = {t: identity(c.list_objects(client, t)) for t in tools}
        tables_before = identity(c.list_objects(client, 'table_operations'))
        relations_before = c.list_objects(client, 'relationship_operations')
        tables = c.list_objects(client, 'table_operations')
        candidates = [t for t in tables if t.get('columnCount', 0) > 0 and not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_'))]
        if not candidates:
            raise RuntimeError('No se encontro una tabla existente apta')
        table = min(candidates, key=lambda t: t.get('columnCount', 1000000))['name']
        report['table'] = table
        for tool in tools:
            objects = [{'tableName': table, 'name': prefix + ('M' if tool == tools[0] else 'C') + str(i), 'expression': str(i)} for i in (1, 2)]
            if any((table, o['name']) in before[tool] for o in objects):
                raise RuntimeError('Colision de nombres temporales')
            attempted.extend((tool, c.object_reference(obj)) for obj in objects)
            for operation, plans in (
                    ('Create', objects),
                    ('Update', [dict(obj, expression=str(i + 10), description='Prueba temporal del cliente Python') for i, obj in enumerate(objects, 1)]),
                    ('Delete', [c.object_reference(obj) for obj in objects])):
                with patch('builtins.input', side_effect=consent):
                    outcomes = c.apply_objects(client, tool, operation, plans)
                if len(outcomes) != 2 or any(state != 'confirmado' for _, state in outcomes):
                    raise RuntimeError(f'{tool}.{operation}: falta confirmacion; no se repetira automaticamente')
                inventory = identity(c.list_objects(client, tool))
                if operation == 'Delete':
                    if any((table, o['name']) in inventory for o in objects):
                        raise AssertionError('La eliminacion no se refleja en List')
                else:
                    for obj, plan in zip(objects, plans):
                        detail = c.get_object(client, tool, obj)
                        if str(detail.get('expression', '')).strip() != plan['expression']:
                            raise AssertionError('La expresion leida no coincide con la enviada')
                        if operation == 'Update' and detail.get('description') != plan['description']:
                            raise AssertionError('La descripcion no coincide')
                        if tool == 'column_operations' and not c.calculated_column(detail):
                            raise AssertionError('El tipo no es columna calculada')
                report['steps'].append({'tool': tool, 'operation': operation, 'objects': 2, 'verifiedByRead': True})
                print('VERIFICADO POR LECTURA:', tool, operation, '2 objetos')
        if any(identity(c.list_objects(client, t)) != before[t] for t in tools):
            raise AssertionError('El inventario final no coincide con el inicial')
        if identity(c.list_objects(client, 'table_operations')) != tables_before:
            raise AssertionError('Cambio inesperado en el inventario de tablas')
        if c.list_objects(client, 'relationship_operations') != relations_before:
            raise AssertionError('Cambio inesperado en las relaciones')
        report['verified'] = True
    except BaseException as exc:
        report['error'] = str(exc)
        raise
    finally:
        try:
            # Check all attempted names, including an ambiguous Create, and remove
            # only names generated by this run. Never delete an existing object.
            if attempted and not client.connection_name:
                instances = c.list_instances(client)
                matches = [i for i, x in enumerate(instances, 1) if x.get('parentWindowTitle') == model]
                if len(matches) == 1:
                    with patch('builtins.input', return_value=str(matches[0])):
                        c.connect(client)
            for tool, obj in reversed(attempted):
                try:
                    present = identity(c.list_objects(client, tool))
                    if (obj['tableName'], obj['name']) in present:
                        with patch('builtins.input', side_effect=consent):
                            outcomes = c.apply_objects(client, tool, 'Delete', [obj])
                        present = identity(c.list_objects(client, tool))
                        if (obj['tableName'], obj['name']) in present:
                            raise RuntimeError('El objeto temporal sigue presente')
                    report['cleanup'].append({'tool': tool, 'name': obj['name'], 'absent': True})
                except BaseException as exc:
                    report['cleanup'].append({'tool': tool, 'name': obj['name'], 'absent': False, 'error': str(exc)})
                    report['verified'] = False
                    print('ERROR DE LIMPIEZA:', tool, obj['name'], exc)
        finally:
            client.close()
            target = Path(__file__).resolve().parents[1] / 'resultado_escrituras.json'
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Informe:', target)
    if not report['verified']:
        raise RuntimeError('La prueba no se completo; revisa resultado_escrituras.json')
    print('ESCRITURAS VERIFICADAS Y OBJETOS TEMPORALES ELIMINADOS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo', required=True)
    parser.add_argument('--confirmar-escrituras', action='store_true', required=True,
                        help='Autoriza crear, modificar y eliminar solo objetos temporales de esta prueba')
    args = parser.parse_args()
    run(args.modelo)


if __name__ == '__main__':
    main()
