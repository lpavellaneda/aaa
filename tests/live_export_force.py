"""Integration test against an explicitly selected disposable Desktop model."""
import contextlib
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def run():
    client = c.MCP(writable=True)
    prefix = '__EXPORT_FORCE_' + uuid.uuid4().hex[:8] + '_'
    report = {'cases': [], 'cleanup': False}
    try:
        instances = c.list_instances(client)
        if len(instances) != 1:
            raise RuntimeError('This test requires exactly one disposable open model')
        c.attach_instance(client, instances[0])
        baseline = c.metadata_export(client)
        if not baseline['complete']:
            raise AssertionError(baseline['errors'])
        report['export'] = {key: len(baseline[key]) for key in ('tables', 'measures', 'columns', 'calculatedColumns', 'relationships')}
        report['cases'].append('full_export')
        table = next(t['name'] for t in baseline['tables'] if not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_')))

        def op(action, suffix, expression=None, **props):
            value = dict(action=action, table=table, name=prefix + suffix, **props)
            if expression is not None:
                value['expression'] = expression
            return value

        def block(operations, force=False):
            answers = [json.dumps({'operations': operations}), 'FIN'] + (['CONFIRMAR FORZAR'] if force else [])
            with patch('builtins.input', side_effect=answers):
                return c.ai_block(client)

        def good(operations, force=False):
            result = block(operations, force)
            if len(result) != len(operations) or any(r['status'] != 'correcta' for r in result) or client.last_block_status != 'completado':
                raise AssertionError(result)
            return result

        def independently_read():
            reader = c.MCP(writable=False)
            try:
                observed = [i for i in c.list_instances(reader) if c.same_instance(instances[0], i)]
                if len(observed) != 1:
                    raise AssertionError('Original model instance unavailable')
                c.attach_instance(reader, observed[0], expected=c.selected_target(client))
                return c.metadata_export(reader)
            finally:
                reader.close()

        good([op('create_measure', 'Base', '1', formatString='0.00', displayFolder='Prueba exportacion', description='Formato completo'),
              op('create_measure', 'Dep', f'[{prefix}Base] + 1'), op('create_column', 'Col', '1')])
        snapshot = c.metadata_export(client)
        measure = next(m for m in snapshot['measures'] if m['name'] == prefix + 'Base')
        assert measure['expression'] == '1' and measure['formatString'] == '0.00'
        assert any(col['name'] == prefix + 'Col' and col['expression'] == '1' for col in snapshot['calculatedColumns'])
        report['cases'].append('export_formats_and_calculated_expression')
        # Default mode still blocks a dependency without writing.
        try:
            block([op('delete_measure', 'Base')])
        except ValueError as exc:
            report['defaultDependencyError'] = str(exc)
        else:
            raise AssertionError('Default deletion accepted a dependency')
        report['cases'].append('default_dependency_protection')
        good([op('delete_measure', 'Base', force=True)], force=True)
        after = independently_read()
        assert not any(m['name'] == prefix + 'Base' for m in after['measures'])
        report['forceDependentAfterDelete'] = [m for m in after['measures'] if m['name'] == prefix + 'Dep']
        report['cases'].append('force_measure_delete_with_dependency')
        # Recreate base to repair a surviving dependent, if the server kept it.
        good([op('create_measure', 'Base', '4')])
        before_error = c.metadata_export(client)
        # Failure after a forced deletion, inside the native transaction.
        original = c.request_call
        def injected(client_arg, tool, operation, **fields):
            if tool == 'measure_operations' and operation == 'Create' and fields.get('definitions', [{}])[0].get('name') == prefix + 'Failure':
                raise RuntimeError('Intentional transport-independent intermediate failure')
            return original(client_arg, tool, operation, **fields)
        with patch.object(c, 'request_call', side_effect=injected):
            result = block([op('delete_measure', 'Base', force=True), op('create_measure', 'Failure', '1')], force=True)
        assert result[-1]['status'] == 'fallida' and client.last_recovery == 'verificada', result
        assert not c.snapshot_differences(before_error, c.metadata_export(client))
        report['cases'].append('native_rollback_forced_delete_verified_full_snapshot')
        # Full CRUD for calculated columns and measures in one ordered block.
        good([op('update_measure', 'Base', '5'), op('update_column', 'Col', '3'),
              op('create_column', 'Col2', '2'), op('delete_column', 'Col2'), op('create_measure', 'Extra', '8'),
              op('delete_measure', 'Extra')])
        report['cases'].append('mixed_measure_and_column_crud')
        good([op('delete_column', 'Col', force=True)], force=True)
        report['cases'].append('force_calculated_column_delete')
        # A forced column deletion can remove a relationship and leave DAX dependents invalid.
        other_table = next(t['name'] for t in baseline['tables'] if t['name'] != table and not t['name'].startswith(('LocalDateTable_', 'DateTableTemplate_')))
        good([op('create_column', 'RelBase', '1'), op('create_column', 'RelDep', c.dax_column_reference(table, prefix + 'RelBase') + ' + 1'),
              op('create_measure', 'RelMeasure', 'SUM(' + c.dax_column_reference(table, prefix + 'RelBase') + ')'),
              dict(action='create_column', table=other_table, name=prefix + 'OtherKey', expression='1')])
        relation_name = prefix + 'Relationship'
        c.request_call(client, 'relationship_operations', 'Create', definitions=[dict(name=relation_name, isActive=False,
            fromTable=table, fromColumn=prefix + 'RelBase', fromCardinality='Many', toTable=other_table,
            toColumn=prefix + 'OtherKey', toCardinality='Many', crossFilteringBehavior='OneDirection')])
        assert any(r['name'] == relation_name for r in c.list_objects(client, 'relationship_operations'))
        before_failure = c.metadata_export(client)
        with patch.object(c, 'request_call', side_effect=injected):
            result = block([op('delete_column', 'RelBase', force=True), op('create_measure', 'Failure', '1')], force=True)
        assert result[-1]['status'] == 'fallida' and client.last_recovery == 'verificada', result
        assert not c.snapshot_differences(before_failure, independently_read())
        report['cases'].append('native_rollback_restores_relationship_and_column_dependents')
        good([op('delete_column', 'RelBase', force=True)], force=True)
        after = independently_read()
        assert not any(r['name'] == prefix + 'RelBase' for r in after['columns'])
        assert not any(r['name'] == relation_name for r in after['relationships'])
        report['columnDependentStates'] = {r['name']: r.get('state') for section in ('measures', 'columns') for r in after[section]
                                          if r['name'] in (prefix + 'RelDep', prefix + 'RelMeasure')}
        report['cases'].append('force_column_cascades_relationship_independent_mcp_verification')
    finally:
        try:
            for relationship in c.list_objects(client, 'relationship_operations'):
                if relationship['name'].startswith(prefix):
                    c.request_call(client, 'relationship_operations', 'Delete', references=[{'name': relationship['name']}])
            # Remove dependents first; only objects created by this test are selected.
            for tool in ('measure_operations', 'column_operations'):
                rows = [r for r in c.list_objects(client, tool) if r['name'].startswith(prefix)]
                rows.sort(key=lambda r: r['name'].endswith('Base'))
                for row in rows:
                    c.request_call(client, tool, 'Delete', **c.build_object_fields(client, tool, 'Delete', row))
            after_cleanup = c.metadata_export(client)
            report['cleanup'] = not c.snapshot_differences(baseline, after_cleanup)
            if not report['cleanup']:
                report['cleanupDifferences'] = c.snapshot_differences(baseline, after_cleanup)
        finally:
            client.close()
            Path('resultado_export_force.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    assert report['cleanup'], report
    print('REAL TEST PASSED:', json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    run()
