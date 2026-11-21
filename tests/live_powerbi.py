"""Prueba real opcional, solo lectura. Requiere extension y un PBIX abierto."""
import argparse
import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo', help='Titulo exacto del modelo (necesario si hay varios)')
    args = parser.parse_args()
    client = c.MCP(writable=False)
    try:
        instances = c.list_instances(client)
        matches = [(i, x) for i, x in enumerate(instances, 1)
                   if not args.modelo or (x.get('parentWindowTitle') or x.get('name')) == args.modelo]
        if len(matches) != 1:
            raise RuntimeError('Abre un PBIX y especifica --modelo si hay varias instancias')
        index, _ = matches[0]
        with patch('builtins.input', return_value=str(index)):
            c.connect(client)
        if not c.ensure_connected(client):
            raise RuntimeError('La conexion no es valida')
        for tool in ('table_operations', 'measure_operations', 'column_operations', 'relationship_operations'):
            rows = c.list_objects(client, tool)
            print(tool + ':', len(rows))
            if rows and tool != 'relationship_operations':
                detail = c.get_object(client, tool, rows[0])
                if detail.get('name') != rows[0]['name']:
                    raise AssertionError('El detalle no corresponde al objeto seleccionado')
                print('  Get: correcto')
            # Verify actual menu rendering without logging model names/expressions.
            with contextlib.redirect_stdout(io.StringIO()), patch('builtins.input', return_value=''):
                if tool != 'column_operations':
                    c.simple_list(client, tool, tool)
        props = c.request_schema(client, 'dax_query_operations')['properties']
        fields = {'query': 'EVALUATE ROW("PruebaCliente", 1)'}
        if 'resultMode' in props:
            fields['resultMode'] = 'Inline'
        result = c.dax_rows(c.request_call(client, 'dax_query_operations', 'Execute', **fields))
        if len(result) != 1 or list(result[0].values()) != [1]:
            raise AssertionError('Resultado DAX inesperado')
        print('DAX Execute: correcto')
        try:
            c.request_call(client, 'dax_query_operations', 'Execute', query='EVALUATE EXPRESION_INVALIDA_DEL_CLIENTE', resultMode='Inline')
        except RuntimeError:
            print('Error DAX: detectado correctamente')
        else:
            raise AssertionError('No se detecto el error DAX')
        print('Prueba real de lectura completada. No se enviaron escrituras.')
    finally:
        client.close()


if __name__ == '__main__':
    main()
