"""Pruebas DAX con datos reales, relaciones y recuperacion; solo en modelo de prueba."""
import argparse
from collections import defaultdict
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliente as c


def qt(name):
    return "'" + name.replace("'", "''") + "'"


def qc(table, column):
    return qt(table) + '[' + column.replace(']', ']]') + ']'


def mr(name):
    return '[' + name.replace(']', ']]') + ']'


def value(row, name):
    return row.get('[' + name + ']', row.get(name))


def close_number(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    if isinstance(expected, int):
        return actual == expected
    return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-6)


class ComplexModelSuite:
    def __init__(self, model):
        self.client = c.MCP(writable=True)
        self.reader = None
        self.model = model
        self.prefix = '__REAL_DAX_' + uuid.uuid4().hex[:10] + '_'
        self.report_path = Path('resultado_modelo_complejo.json')
        self.report = {'tests': [], 'blocks': [], 'expressions': {}, 'unexpectedFailures': [],
                       'expectedNegativeCases': 0, 'valueChecks': 0, 'cleanupVerified': False,
                       'numericTolerance': {'relative': 1e-12, 'absolute': 1e-6}}
        self.tables = {}
        self.columns = []

    def save(self):
        self.report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')

    def case(self, name, fn):
        try:
            fn()
            self.report['tests'].append({'name': name, 'passed': True})
            print('PRUEBA SUPERADA:', name, flush=True)
        except Exception as exc:
            self.report['tests'].append({'name': name, 'passed': False, 'error': str(exc)})
            self.report['unexpectedFailures'].append({'name': name, 'error': str(exc)})
            self.save()
            raise
        self.save()

    def fresh_reader(self):
        if self.reader:
            self.reader.close()
        self.reader = c.MCP(writable=False)
        instances = c.list_instances(self.reader)
        choices = [i for i, row in enumerate(instances, 1) if c.same_instance(self.client.target_identity['instance'], row)]
        if len(choices) != 1:
            raise RuntimeError('No se puede verificar el modelo original desde el lector independiente')
        with patch('builtins.input', return_value=str(choices[0])):
            c.connect(self.reader)
        if not c.same_target(self.client.target_identity, self.reader.target_identity):
            raise RuntimeError('El lector independiente no apunta a la misma base de datos')

    def query(self, query, reader=False):
        active = self.reader if reader else self.client
        return c.dax_rows(c.request_call(active, 'dax_query_operations', 'Execute',
                                       query=query, resultMode='Inline', maxRows=100000))

    def compare(self, actual, expected, label):
        self.report['valueChecks'] += 1
        if not close_number(actual, expected):
            raise AssertionError(f'{label}: esperado {expected!r}, observado {actual!r}')

    def measures(self, expected, context=None):
        names = list(expected)
        row = 'ROW(' + ', '.join(json.dumps('V' + str(i)) + ', ' + mr(name) for i, name in enumerate(names)) + ')'
        query = 'EVALUATE ' + (f'CALCULATETABLE({row}, {context})' if context else row)
        values = self.query(query, reader=True)
        if len(values) != 1:
            raise AssertionError('La consulta de verificacion no devolvio una fila')
        for i, name in enumerate(names):
            actual = value(values[0], 'V' + str(i))
            self.compare(actual, expected[name], name)
        self.report.setdefault('evaluations', []).append({'measures': names, 'context': context,
                'expected': expected, 'observed': {name: value(values[0], 'V' + str(i)) for i, name in enumerate(names)}})

    def op(self, action, name, expression=None, table=None, **properties):
        result = {'action': action, 'name': name, 'table': table or self.fact, **properties}
        if expression is not None:
            result['expression'] = expression
        return result

    def block(self, name, operations, expected_failure=False, query_expectations=None):
        expanded = c.parse_ai_block(json.dumps({'operations': operations}))
        captured = {}
        original = c.request_call
        query_expectations = query_expectations or {}
        def record(active, tool, operation, **fields):
            result = original(active, tool, operation, **fields)
            if tool == 'dax_query_operations' and fields.get('query') in query_expectations:
                captured[fields['query']] = c.dax_rows(result)
            return result
        with patch('builtins.input', side_effect=[json.dumps({'operations': operations}), 'FIN']) as inp, \
             patch.object(c, 'request_call', side_effect=record):
            result = c.ai_block(self.client)
        if inp.call_count != 2:
            raise AssertionError('La opcion 13 pidio confirmaciones adicionales')
        failed = self.client.last_block_status == 'fallido'
        if failed != expected_failure:
            raise AssertionError(f'{name}: estado inesperado {self.client.last_block_status}; recuperacion {self.client.last_recovery}')
        if expected_failure:
            if self.client.last_recovery != 'verificada':
                raise AssertionError('La recuperacion no quedo verificada')
            self.report['expectedNegativeCases'] += 1
            if not any(row['status'] == 'fallida' for row in result):
                raise AssertionError('No se identifico la operacion con error')
        elif len(result) != len(expanded):
            raise AssertionError('No se ejecutaron todas las instrucciones')
        self.fresh_reader()
        if expected_failure:
            backup=json.loads(Path(self.client.last_backup_path).read_text(encoding='utf-8'))
            for target in backup['targets']:
                actual=c.target_state(self.reader,target['tool'],target['reference'])
                if c.stable_state(actual)!=c.stable_state(target['before']):
                    raise AssertionError('El lector independiente encontro diferencias tras recuperar: '+target['reference']['name'])
                if actual and target['before'] and not target['before'].get('errorMessage'):
                    c.check_semantic_state(actual)
                    if target['tool']=='column_operations' and c.calculated_column(target['before']):
                        c.verify_column_storage(self.reader,target['reference'])
                self.report['independentRecoveryTargets']=self.report.get('independentRecoveryTargets',0)+1
        else:
            final = {}
            for item in expanded:
                if item['action'] == 'execute_dax':
                    continue
                tool = c.AI_ACTIONS[item['action']][0]
                key = (tool, item['table'], item['name'])
                if item['action'].startswith('delete_'):
                    final[key] = None
                else:
                    final[key] = dict(final.get(key) or {}, **c.ai_object(item))
            for (tool, table, objname), expected in final.items():
                ref = {'tableName': table, 'name': objname}
                actual = c.target_state(self.reader, tool, ref)
                if expected is None:
                    if actual is not None:
                        raise AssertionError('El objeto eliminado sigue visible desde otra sesion')
                else:
                    if actual is None:
                        raise AssertionError('El objeto escrito no existe desde otra sesion')
                    c.check_semantic_state(actual)
                    for key in ('expression', 'description', 'formatString', 'displayFolder'):
                        if key in expected and str(actual.get(key) or '').replace('\r\n', '\n').strip() != str(expected[key]).strip():
                            raise AssertionError(f'{table}[{objname}]: propiedad {key} distinta despues de confirmar')
        for query, expected in query_expectations.items():
            rows = captured.get(query)
            if not rows:
                raise AssertionError('La consulta intermedia no se ejecuto')
            self.compare(value(rows[0], 'Valor'), expected, 'consulta intermedia')
        self.report['blocks'].append({'name': name, 'operations': len(expanded), 'expectedFailure': expected_failure,
                                     'status': self.client.last_block_status, 'recovery': self.client.last_recovery,
                                     'results': result})
        self.save()

    def reject(self, name, operations, text):
        # Validate against the real catalog, without sending any model write.
        try:
            plan, _ = c.validate_ai_block(self.client, c.parse_ai_block(json.dumps({'operations': operations})))
            c.check_engine_dependencies(plan, c.dependency_report(self.client))
        except ValueError as exc:
            if text.casefold() not in str(exc).casefold():
                raise AssertionError(f'Rechazo no esperado: {exc}')
            self.report['expectedNegativeCases'] += 1
            self.report.setdefault('rejections', []).append({'name': name, 'message': str(exc)})
            return
        raise AssertionError('Se acepto un bloque que debia rechazarse antes de escribir')

    def inspect(self):
        instances = c.list_instances(self.client)
        choices = [i for i, row in enumerate(instances, 1) if not self.model or row.get('parentWindowTitle') == self.model]
        if len(choices) != 1:
            raise RuntimeError('Especifica el titulo exacto de un unico modelo de pruebas')
        with patch('builtins.input', return_value=str(choices[0])):
            c.connect(self.client)
        self.columns = c.list_objects(self.client, 'column_operations')
        self.tables = {row['name']: dict(row) for row in c.list_objects(self.client, 'table_operations')}
        self.relationships = c.list_objects(self.client, 'relationship_operations')
        self.initial_measures = {(row['tableName'], row['name']) for row in c.list_objects(self.client, 'measure_operations')}
        self.initial_columns = {(row['tableName'], row['name']) for row in self.columns}
        self.initial_relationships = self.relationships
        for name in self.tables:
            self.tables[name]['rows'] = value(self.query(f'EVALUATE ROW("N", COUNTROWS({qt(name)}))')[0], 'N') or 0
        def numeric(table):
            return [row for row in self.columns if row['tableName'] == table and row.get('dataType') in ('Double', 'Decimal', 'Int64') and not row.get('isCalculated')]
        relations = [r for r in self.relationships if r.get('isActive') and r.get('fromCardinality') == 'Many' and r.get('toCardinality') == 'One'
                     and numeric(r['fromTable']) and numeric(r['toTable']) and self.tables[r['fromTable']]['rows'] > 0]
        if not relations:
            raise RuntimeError('Se requiere una relacion activa muchos-a-uno con datos y columnas numericas; no se inventaran nombres')
        def relation_rank(r):
            col = next((col for col in self.columns if col['tableName'] == r['toTable'] and col['name'] == r['toColumn']), {})
            return (self.tables[r['fromTable']]['rows'], col.get('dataType') == 'DateTime', self.tables[r['toTable']]['rows'])
        self.relation = max(relations, key=relation_rank)
        self.fact, self.dim = self.relation['fromTable'], self.relation['toTable']
        amounts = numeric(self.fact)
        amounts.sort(key=lambda row: not any(word in row['name'].casefold() for word in ('venta', 'importe', 'amount', 'monto')))
        self.amount = amounts[0]['name']
        dimcols = numeric(self.dim)
        dimcols.sort(key=lambda row: not any(word in row['name'].casefold() for word in ('year', 'año', 'anio')))
        self.dim_number = dimcols[0]['name']
        others = [name for name in self.tables if name not in (self.fact, self.dim) and numeric(name) and self.tables[name]['rows'] > 0]
        if not others:
            raise RuntimeError('No hay otra tabla numerica para probar formulas entre tablas')
        self.other = max(others, key=lambda name: self.tables[name]['rows'])
        other_columns = numeric(self.other)
        # Prefer homogeneous units when names explicitly expose currency. This is
        # test-data selection, not a financial interpretation of the model.
        currency_groups = (('sol', 'pen'), ('dolar', 'dólar', 'usd'), ('eur',))
        tokens = next((group for group in currency_groups if any(token in self.amount.casefold() for token in group)), ())
        compatible = [col for col in other_columns if any(token in col['name'].casefold() for token in tokens)]
        self.other_amount = (compatible or other_columns)[0]['name']
        f, amount = qt(self.fact), qc(self.fact, self.amount)
        group = f'''EVALUATE SELECTCOLUMNS(
SUMMARIZECOLUMNS({qc(self.dim,self.relation['toColumn'])}, {qc(self.dim,self.dim_number)},
"__Rows", COUNTROWS({f}), "__Total", SUM({amount}), "__Count", COUNT({amount}),
"__Pos", SUMX(FILTER({f}, {amount} > 0), {amount})),
"Clave", {qc(self.dim,self.relation['toColumn'])}, "Grupo", {qc(self.dim,self.dim_number)},
"Filas", [__Rows], "Total", [__Total], "NoVacios", [__Count], "Positivo", [__Pos])'''
        grouped = self.query(group)
        self.total = math.fsum(float(value(row, 'Total') or 0) for row in grouped)
        self.rowcount = sum(int(value(row, 'Filas') or 0) for row in grouped)
        nonblank = sum(int(value(row, 'NoVacios') or 0) for row in grouped)
        self.average = self.total / nonblank if nonblank else None
        self.positive = math.fsum(float(value(row, 'Positivo') or 0) for row in grouped)
        self.related = math.fsum(float(value(row, 'Total') or 0) * float(value(row, 'Grupo') or 0) for row in grouped)
        self.compare(self.rowcount, self.tables[self.fact]['rows'], 'filas agrupadas')
        distinct = self.query(f'EVALUATE DISTINCT(SELECTCOLUMNS({f}, "Clave", {qc(self.fact,self.relation["fromColumn"])}))')
        if len(distinct) >= 100000:
            raise RuntimeError('El dominio de claves supera el limite de verificacion')
        self.distinct = len(distinct)
        self.other_sum = value(self.query(f'EVALUATE ROW("V", SUM({qc(self.other,self.other_amount)}))')[0], 'V')
        groups = defaultdict(list)
        for row in grouped:
            if isinstance(value(row, 'Grupo'), (int, float)):
                groups[value(row, 'Grupo')].append(row)
        self.selected_group = max(groups, key=lambda key: sum(value(row, 'Filas') or 0 for row in groups[key]))
        self.filtered_total = math.fsum(float(value(row, 'Total') or 0) for row in groups[self.selected_group])
        self.filtered_rows = sum(int(value(row, 'Filas') or 0) for row in groups[self.selected_group])
        self.filtered_positive = math.fsum(float(value(row, 'Positivo') or 0) for row in groups[self.selected_group])
        self.context = qc(self.dim, self.dim_number) + ' = ' + str(self.selected_group)
        if self.filtered_rows == self.rowcount:
            raise RuntimeError('El contexto elegido no limita las filas; se necesita otro grupo para probar filtros')
        self.report['inspection'] = {'model': self.client.model_name, 'tables': list(self.tables.values()),
            'columnCount': len(self.columns), 'measureCount': len(self.initial_measures), 'relationships': self.relationships,
            'fact': self.fact, 'amount': self.amount, 'dimension': self.dim, 'dimensionNumber': self.dim_number,
            'otherTable': self.other, 'otherColumn': self.other_amount, 'selectedRelationship': self.relation}
        self.report['baseline'] = {'sumFromGroupedData': self.total, 'rows': self.rowcount, 'average': self.average,
            'distinctKeys': self.distinct, 'positive': self.positive, 'relatedWeighted': self.related,
            'otherSum': self.other_sum, 'filterGroup': self.selected_group,
            'filteredTotal': self.filtered_total, 'filteredRows': self.filtered_rows}
        self.fresh_reader()

    def run(self):
        try:
            self.case('inspeccion_y_linea_base_independiente', self.inspect)
            f, amount = qt(self.fact), qc(self.fact, self.amount)
            dimnum = qc(self.dim, self.dim_number)
            self.n = {key: self.prefix + key for key in ('SUM', 'AVG', 'COUNT', 'DISTINCT', 'VAR', 'DIVIDE', 'SUMX', 'FILTER', 'ALL', 'REMOVE')}
            expressions = {'SUM': f'SUM({amount})', 'AVG': f'AVERAGE({amount})', 'COUNT': f'COUNTROWS({f})',
                'DISTINCT': f'DISTINCTCOUNT({qc(self.fact,self.relation["fromColumn"])})',
                'VAR': f'VAR escala = 2\nRETURN {mr(self.n["SUM"])} * escala',
                'DIVIDE': f'DIVIDE({mr(self.n["SUM"])}, {mr(self.n["COUNT"])}, 0)',
                'SUMX': f'SUMX({f}, {amount} * 2)',
                'FILTER': f'CALCULATE({mr(self.n["SUM"])}, FILTER(ALL({amount}), {amount} > 0))',
                'ALL': f'CALCULATE({mr(self.n["SUM"])}, ALL({f}))',
                'REMOVE': f'CALCULATE({mr(self.n["SUM"])}, REMOVEFILTERS({qt(self.dim)}))'}
            self.report['expressions'].update({self.n[key]: expr for key, expr in expressions.items()})
            self.case('bloque_10_creaciones_complejas', lambda: self.block('10_creaciones', [self.op('create_measure', self.n[key], expr) for key, expr in expressions.items()]))
            expected = {self.n['SUM']: self.total, self.n['AVG']: self.average, self.n['COUNT']: self.rowcount,
                self.n['DISTINCT']: self.distinct, self.n['VAR']: self.total*2, self.n['DIVIDE']: self.total/self.rowcount,
                self.n['SUMX']: self.total*2, self.n['FILTER']: self.positive, self.n['ALL']: self.total, self.n['REMOVE']: self.total}
            self.case('evaluacion_numerica_10_medidas', lambda: self.measures(expected))
            self.case('relacion_y_contexto_de_filtro', lambda: self.measures({self.n['SUM']:self.filtered_total,
                self.n['COUNT']: self.filtered_rows, self.n['VAR']:self.filtered_total*2,
                self.n['FILTER']:self.filtered_positive, self.n['ALL']:self.total, self.n['REMOVE']:self.total}, self.context))
            self.case('motor_confirma_dependencia_VAR_RETURN', self.check_dependency)
            self.case('rechaza_eliminar_medida_referenciada', lambda: self.reject('eliminar_base', [self.op('delete_measure', self.n['SUM'])], 'dependencia'))
            self.case('rechaza_dependencia_nueva_en_mismo_bloque', lambda: self.reject('return_mismo_bloque', [
                self.op('create_measure', self.prefix+'A', f'SUM({amount})'),
                self.op('create_measure', self.prefix+'B', f'VAR n=2 RETURN {mr(self.prefix+"A")}*n'),
                self.op('delete_measure', self.prefix+'A')], 'dependencia'))
            self.case('rechaza_referencia_a_creacion_posterior', lambda: self.reject('orden', [
                self.op('create_measure', self.prefix+'B', f'VAR n=2 RETURN {mr(self.prefix+"A")}*n'),
                self.op('create_measure', self.prefix+'A', f'SUM({amount})')], 'despues'))
            self.case('rechaza_ciclo_indirecto_VAR_RETURN', lambda: self.reject('ciclo', [
                self.op('update_measure', self.n['AVG'], f'VAR n=2 RETURN {mr(self.n["SUMX"])}*n'),
                self.op('update_measure', self.n['SUMX'], f'VAR n=3 RETURN {mr(self.n["AVG"])}*n')], 'circular'))
            extra = [self.prefix+'EXTRA'+str(i) for i in range(10)]
            extras = [f'SUM({amount})', f'AVERAGE({amount})', f'COUNTROWS({f})',
                f'SUMX(FILTER({f}, RELATED({dimnum}) = {self.selected_group}), {amount})',
                f'SUM({amount}) + SUM({qc(self.other,self.other_amount)})',
                f'CALCULATE(SUM({amount}), {self.context})',
                f'VAR t=SUM({amount}) VAR n=COUNTROWS({f}) RETURN DIVIDE(t,n,0)',
                f'CALCULATE(SUM({amount}), FILTER(ALL({qt(self.dim)}), {dimnum} = {self.selected_group}))',
                f'SUMX(FILTER({f}, {amount} > 0), {amount})', f'DIVIDE(SUM({amount}), SUM({qc(self.other,self.other_amount)}), 0)']
            self.col, self.col_related = self.prefix+'COL', self.prefix+'RELATED_COL'
            ops = [self.op('create_measure', name, expr, table=self.other if i%2 else self.fact) for i,(name,expr) in enumerate(zip(extra,extras))]
            ops += [self.op('update_measure', self.n['SUM'], f'SUM({amount}) * 2'),
                    self.op('update_measure', self.n['AVG'], f'AVERAGE({amount}) * 2'),
                    self.op('update_measure', self.n['COUNT'], f'COUNTROWS({f}) * 2'),
                    self.op('update_measure', self.n['SUMX'], f'SUMX({f}, {amount} * 3)')]
            ops += [self.op('create_column', self.col, amount+' * 2'), self.op('create_column', self.col_related, f'RELATED({dimnum})'),
                    self.op('update_column', self.col, amount+' * 3')]
            ops += [self.op('delete_measure', extra[i], table=self.other if i%2 else self.fact) for i in range(3)]
            self.report['expressions'].update(dict(zip(extra,extras)))
            self.case('bloque_20_mixto_con_columnas_y_tablas_distintas', lambda: self.block('20_mixtas', ops))
            self.case('evaluacion_tras_modificar_dependencias', lambda: self.measures({self.n['SUM']:self.total*2,
                self.n['AVG']:self.average*2,self.n['COUNT']:self.rowcount*2,self.n['VAR']:self.total*4,
                self.n['DIVIDE']:self.total/self.rowcount,self.n['SUMX']:self.total*3,
                extra[3]:self.filtered_total,extra[4]:self.total+self.other_sum,extra[5]:self.filtered_total,
                extra[6]:self.total/self.rowcount,extra[7]:self.filtered_total,extra[8]:self.positive,
                extra[9]:self.total/self.other_sum if self.other_sum else 0}))
            self.case('columnas_calculadas_y_RELATED_con_datos', lambda: self.check_columns(3))
            colmeasure=self.prefix+'SUM_COLUMN'
            self.case('modificar_columna_con_medida_dependiente', lambda: self.block('columna_dependiente', [
                self.op('create_measure', colmeasure, f'SUM({qc(self.fact,self.col)})'),
                self.op('update_column', self.col, amount+' * 4')]))
            self.case('resultado_dependencia_columna_medida', lambda: self.measures({colmeasure:self.total*4}))
            batch=[self.prefix+'BATCH'+str(i) for i in range(50)]
            batch_ops=[]
            batch_values={}
            for i,name in enumerate(batch):
                if i%5==0:
                    expr=f'SUM({amount}) * {i+1}'
                    expected_value=self.total*(i+1)
                else:
                    expr=f'VAR n=COUNTROWS({f}) RETURN {mr(batch[i-1])} + DIVIDE(n, {i+1}, 0)'
                    expected_value=batch_values[batch[i-1]]+self.rowcount/(i+1)
                batch_ops.append(self.op('create_measure',name,expr,table=self.other if i%2 else self.fact))
                batch_values[name]=expected_value
                self.report['expressions'][name]=expr
            self.case('bloque_50_interdependiente_entre_tablas', lambda: self.block('50_interdependientes', batch_ops))
            self.case('evaluacion_numerica_50_medidas', lambda: self.measures(batch_values))
            col3=self.prefix+'MIX_COL'
            mix=self.prefix+'MIX_MEASURE'
            dep=self.prefix+'MIX_DEP'
            midquery=f'EVALUATE ROW("Valor", {mr(dep)})'
            mixops=[self.op('create_measure',mix,f'SUM({amount})',table=self.other),
                    self.op('update_measure',mix,f'SUM({amount})*4',table=self.other),
                    self.op('create_column',col3,amount+' * 4'), self.op('update_column',col3,amount+' * 5'),
                    self.op('delete_measure',batch[4]),self.op('delete_measure',batch[9],table=self.other),
                    self.op('delete_measure',mix,table=self.other),
                    self.op('create_measure',dep,f'{mr(self.n["SUM"])}+SUM({qc(self.other,self.other_amount)})'),
                    {'action':'execute_dax','query':midquery},self.op('delete_measure',dep)]
            self.case('bloque_10_con_consulta_intermedia', lambda: self.block('10_mixtas',mixops,query_expectations={midquery:self.total*2+self.other_sum}))
            self.case('columna_modificada_en_bloque_mixto', lambda: self.check_column_value(col3,5))
            self.error_cases()
        finally:
            try:
                if self.tables:
                    self.cleanup()
            finally:
                if self.reader:
                    self.reader.close()
                self.client.close()
                self.report['testCount']=len(self.report['tests'])
                self.report['passed']=sum(row['passed'] for row in self.report['tests'])
                self.report['failed']=sum(not row['passed'] for row in self.report['tests'])
                self.save()
        print('SUITE DAX REAL COMPLETADA:',json.dumps({k:self.report[k] for k in ('testCount','passed','failed','expectedNegativeCases','valueChecks','cleanupVerified')},ensure_ascii=True))

    def check_dependency(self):
        report=c.dependency_report(self.client)
        if not report['engine'] or not any(row.get('[OBJECT]')==self.n['VAR'] and row.get('[REFERENCED_OBJECT]')==self.n['SUM'] for row in report['rows']):
            raise AssertionError('El motor no confirmo la dependencia entre medidas')

    def check_columns(self, multiplier):
        self.check_column_value(self.col,multiplier)
        row=self.query(f'EVALUATE ROW("V", SUMX({qt(self.fact)}, {qc(self.fact,self.amount)} * {qc(self.fact,self.col_related)}))',reader=True)[0]
        self.compare(value(row,'V'),self.related,'columna RELATED')

    def check_column_value(self,name,multiplier):
        row=self.query(f'EVALUATE ROW("V", SUM({qc(self.fact,name)}))',reader=True)[0]
        self.compare(value(row,'V'),self.total*multiplier,name)

    def error_cases(self):
        amount=qc(self.fact,self.amount)
        cases=[('columna_inexistente',f'SUM({qc(self.fact,self.prefix+"NO_COLUMN")})'),
               ('medida_inexistente',mr(self.prefix+'NO_MEASURE')),
               ('formula_invalida','SUM(')]
        for label,expression in cases:
            invalid=self.prefix+'ERROR_'+label
            good=self.prefix+'GOOD_'+label
            operations=[self.op('update_measure',self.n['SUM'],f'SUM({amount})*7'),
                        self.op('delete_measure',self.n['AVG']),self.op('create_measure',good,f'AVERAGE({amount})'),
                        self.op('create_measure',invalid,expression)]
            self.case('recuperacion_'+label,lambda label=label,ops=operations:self.block(label,ops,expected_failure=True))
            self.case('verificacion_recuperacion_'+label,lambda:self.measures({self.n['SUM']:self.total*2,
                self.n['AVG']:self.average*2,self.n['VAR']:self.total*4}))
        good=self.prefix+'GOOD_QUERY'
        ops=[self.op('update_measure',self.n['SUM'],f'SUM({amount})*9'),
             self.op('create_measure',good,f'COUNTROWS({qt(self.fact)})'),
             {'action':'execute_dax','query':f'EVALUATE ROW("Valor", {mr(self.prefix+"MISSING_QUERY")})'}]
        self.case('error_DAX_despues_de_escrituras_correctas',lambda:self.block('error_consulta',ops,expected_failure=True))
        self.case('verificacion_recuperacion_consulta',lambda:self.measures({self.n['SUM']:self.total*2,self.n['VAR']:self.total*4}))
        invalid_column=self.prefix+'BAD_COLUMN'
        column_ops=[self.op('update_column',self.col,amount+' * 99'),
                    self.op('update_measure',self.n['SUM'],f'SUM({amount})*8'),
                    self.op('create_column',invalid_column,qc(self.fact,self.prefix+'NO_COLUMN'))]
        self.case('error_columna_y_recuperacion_de_datos_calculados',lambda:self.block('error_columna',column_ops,expected_failure=True))
        self.case('datos_columna_restaurada_y_medida_dependiente',lambda:(
            self.check_column_value(self.col,4),self.measures({self.n['SUM']:self.total*2,
            self.prefix+'SUM_COLUMN':self.total*4,self.n['VAR']:self.total*4})))
        deleted_column_ops=[self.op('delete_column',self.col_related),
                            self.op('update_measure',self.n['SUM'],f'SUM({amount})*9'),
                            self.op('create_measure',self.prefix+'BAD_AFTER_COLUMN_DELETE',f'SUM({qc(self.fact,self.prefix+"ABSENT")})')]
        self.case('recuperacion_columna_calculada_eliminada',lambda:self.block('restaurar_columna_eliminada',deleted_column_ops,expected_failure=True))
        self.case('datos_RELATED_tras_recrear_columna',lambda:self.check_related_column())

    def check_related_column(self):
        row=self.query(f'EVALUATE ROW("V", SUMX({qt(self.fact)}, {qc(self.fact,self.amount)} * {qc(self.fact,self.col_related)}))',reader=True)[0]
        self.compare(value(row,'V'),self.related,'RELATED despues de recrear columna')

    def cleanup(self):
        # Order deletions from leaves to prerequisites, using only generated names.
        rows=[r for r in c.list_objects(self.client,'measure_operations') if r['name'].startswith(self.prefix)]
        details={r['name']:c.get_object(self.client,'measure_operations',r) for r in rows}
        remaining=set(details)
        ordered=[]
        while remaining:
            referenced={name for source in remaining for _,name in c.dax_references(details[source].get('expression') or '')}
            leaves=sorted(name for name in remaining if name.casefold() not in referenced)
            if not leaves:
                raise RuntimeError('No hay orden de limpieza sin ciclos; se conservan los respaldos')
            ordered.extend(leaves)
            remaining.difference_update(leaves)
        ops=[self.op('delete_measure',name,table=details[name]['tableName']) for name in ordered]
        columns=[r for r in c.list_objects(self.client,'column_operations') if r['name'].startswith(self.prefix)]
        ops += [self.op('delete_column',r['name'],table=r['tableName']) for r in columns]
        if ops:
            self.block('limpieza_final',ops)
        self.fresh_reader()
        final_measures={(r['tableName'],r['name']) for r in c.list_objects(self.reader,'measure_operations')}
        final_columns={(r['tableName'],r['name']) for r in c.list_objects(self.reader,'column_operations')}
        if final_measures!=self.initial_measures or final_columns!=self.initial_columns:
            raise AssertionError('El inventario final de objetos no coincide con el original')
        if c.list_objects(self.reader,'relationship_operations')!=self.initial_relationships:
            raise AssertionError('Las relaciones existentes cambiaron')
        self.report['cleanupVerified']=True
        self.report['tests'].append({'name':'limpieza_e_inventario_original_verificados','passed':True})
        self.save()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modelo',help='Titulo exacto; puede omitirse si solo hay una instancia')
    parser.add_argument('--confirmar-escrituras',action='store_true',required=True)
    args=parser.parse_args()
    ComplexModelSuite(args.modelo).run()
