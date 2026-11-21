import argparse
import csv
import difflib
import json
import time
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

EXTENSIONS = Path.home() / '.vscode' / 'extensions'
MATCHES = sorted(
    [p for pattern in ('analysis-services.powerbi-modeling-mcp-*-win32-x64/server/powerbi-modeling-mcp.exe',
                       'microsoft.powerbi-modeling-mcp-*-win32-x64/server/powerbi-modeling-mcp.exe')
     for p in EXTENSIONS.glob(pattern)],
    key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p.parent.parent.name)), reverse=True)
SERVER = MATCHES[0] if MATCHES else None

class MCP:
    def __init__(self, writable=False, server=None, debug=False):
        self.writable = writable
        self.debug = debug
        server = Path(server) if server else SERVER
        if not server or not server.is_file():
            raise RuntimeError('No se encontro powerbi-modeling-mcp.exe en VS Code')
        args = [str(server), '--start', '--readwrite' if writable else '--readonly']
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                        errors='replace', bufsize=1)
        self.inbox = queue.Queue()
        self.counter = 0
        self.connection_name = None
        self.instance = None
        self.model_name = None
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        self.call('initialize', {'protocolVersion': '2025-06-18',
                                 'capabilities': {'elicitation': {}},
                                 'clientInfo': {'name': 'powerbi-python-menu', 'version': '3.0'}})
        self.notify('notifications/initialized')
        self.tools = {}
        cursor = None
        seen = set()
        while True:
            page = self.call('tools/list', {'cursor': cursor} if cursor else {})
            self.tools.update({t['name']: t for t in page.get('tools', [])})
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError('Paginacion MCP repetida')
            seen.add(cursor)

    def _read(self):
        for line in self.process.stdout:
            try:
                msg = json.loads(line)
                if isinstance(msg, dict):
                    self.inbox.put(msg)
            except json.JSONDecodeError:
                pass
        self.inbox.put({'_closed': True})

    def _stderr(self):
        for line in self.process.stderr:
            if self.debug or re.search(r'\b(error|exception|failed|failure|fail)\b|IsError\s*[=:]\s*True', line, re.IGNORECASE):
                print('[Servidor]', line.rstrip()[:200])

    def send(self, obj):
        if self.process.poll() is not None:
            raise RuntimeError('El servidor MCP se cerro')
        self.process.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def notify(self, method, params=None):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params or {}})

    def call(self, method, params, timeout=180):
        self.counter += 1
        reqid = self.counter
        self.send({'jsonrpc': '2.0', 'id': reqid, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("El servidor no respondio; el resultado es incierto. No se reintentara automaticamente.")
            try:
                response = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError('El servidor no respondio; resultado incierto. No se reintentara automaticamente.') from None
            if response.get('_closed'):
                raise RuntimeError('El servidor MCP se cerro')
            if 'method' in response:
                self._server_request(response)
                continue
            if response.get('id') != reqid:
                continue
            if 'error' in response:
                raise RuntimeError(str(response['error']))
            return response.get('result', {})

    def _server_request(self, message):
        # Server consent prompts belong to the server; never accept silently.
        if 'id' not in message:
            return
        if message.get('method') == 'ping':
            self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': {}})
        elif message.get('method') == 'elicitation/create':
            params = message.get('params', {})
            print('\nSolicitud de consentimiento del servidor:', params.get('message', ''))
            schema = params.get('requestedSchema', {})
            try:
                content = collect_fields(schema)
                validate_schema(schema, schema, content, 'consentimiento')
                action = 'accept' if input('Autorizar esta solicitud del servidor? (s/N): ').strip().lower() == 's' else 'decline'
                result = {'action': action}
                if action == 'accept':
                    result['content'] = content
            except (ValueError, RuntimeError):
                result = {'action': 'decline'}
            self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
        else:
            self.send({'jsonrpc': '2.0', 'id': message['id'],
                       'error': {'code': -32601, 'message': 'Solicitud no soportada por este cliente'}})

    def tool(self, name, args):
        enforce_policy(self, name, args)
        result = self.call('tools/call', {'name': name, 'arguments': args})
        if self.debug:
            pretty(result)
        return unwrap_result(result)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                stream.close()


def pretty(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def tool_schema(client, tool):
    if tool not in client.tools:
        raise RuntimeError('Herramienta no disponible: ' + tool)
    return client.tools[tool].get('inputSchema', {})


READ_OPERATIONS = {
    'connection_operations': {'Help', 'ListLocalInstances', 'Connect', 'GetConnection', 'ListConnections'},
    'table_operations': {'Help', 'List', 'Get', 'GetSchema', 'ExportTMDL', 'ExportTMSL'},
    'measure_operations': {'Help', 'List', 'Get', 'ExportTMDL'},
    'column_operations': {'Help', 'List', 'Get', 'ExportTMDL'},
    'relationship_operations': {'Help', 'List', 'Get', 'Find', 'ExportTMDL'},
    'dax_query_operations': {'Help', 'Execute', 'Validate'},
}


def enforce_policy(client, tool, args):
    operation = args.get('request', {}).get('operation')
    if not client.writable and operation not in READ_OPERATIONS.get(tool, set()):
        raise RuntimeError('Operacion bloqueada en modo solo lectura')


def decode_json(value):
    for _ in range(12):
        if not isinstance(value, str):
            break
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            break
        if decoded == value:
            break
        value = decoded
    return value


def unwrap_result(value):
    value = decode_json(value)
    if isinstance(value, list):
        return [unwrap_result(item) for item in value]
    if not isinstance(value, dict):
        return value
    if (value.get('isError') or value.get('success') is False or value.get('error') or
            str(value.get('status', '')).casefold() in ('failed', 'error', 'failure')):
        raise RuntimeError(error_message(value))
    if 'content' in value:
        values = []
        for item in value.get('content', []):
            if item.get('type') == 'text':
                values.append(unwrap_result(item.get('text', '')))
            elif item.get('type') == 'resource':
                values.append(unwrap_result(item.get('resource', {}).get('text', '')))
        if 'structuredContent' in value:
            return unwrap_result(value['structuredContent'])
        return values[0] if len(values) == 1 else values or value
    return {key: unwrap_result(item) if isinstance(item, (dict, list)) or key in ('data', 'result')
            else item for key, item in value.items()}


def error_message(value):
    if isinstance(value, str):
        parsed = decode_json(value)
        return error_message(parsed) if not isinstance(parsed, str) else parsed
    if isinstance(value, list):
        return '; '.join(error_message(x) for x in value)
    if isinstance(value, dict):
        for key in ('message', 'errorMessage', 'error', 'content', 'text', 'errors'):
            if value.get(key):
                return error_message(value[key])
    return 'El servidor informo un error; consulta --debug para ver la respuesta completa'


def resolve_schema(root, schema):
    if not isinstance(schema, dict):
        return {}
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/'):
            raise RuntimeError('Referencia de esquema externa no soportada')
        target = root
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        return resolve_schema(root, target)
    for key in ('anyOf', 'oneOf'):
        if key in schema:
            choices = [resolve_schema(root, item) for item in schema[key] if item.get('type') != 'null']
            if len(choices) != 1:
                raise RuntimeError('Esquema ambiguo; consulta herramientas avanzadas')
            return choices[0]
    if 'allOf' in schema:
        merged = {'properties': {}, 'required': []}
        for part in schema['allOf']:
            part = resolve_schema(root, part)
            merged['properties'].update(part.get('properties', {}))
            merged['required'].extend(part.get('required', []))
        return merged
    return schema


def request_schema(client, tool):
    root = tool_schema(client, tool)
    return resolve_schema(root, root.get('properties', {}).get('request', {}))


def operation_supported(client, tool, operation):
    root = tool_schema(client, tool)
    spec = resolve_schema(root, request_schema(client, tool).get('properties', {}).get('operation', {}))
    if spec.get('enum'):
        return operation in spec['enum']
    description = spec.get('description') or client.tools[tool].get('description', '')
    return bool(re.search(r'\b' + re.escape(operation) + r'\b', description))


def validate_schema(root, schema, value, path='request'):
    schema = resolve_schema(root, schema)
    if value is None and (schema.get('type') == 'null' or 'null' in schema.get('type', [])):
        return
    typ = schema.get('type')
    if isinstance(typ, list):
        typ = next((t for t in typ if t != 'null'), None)
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool,
             'integer': int, 'number': (int, float)}
    if typ in types and (not isinstance(value, types[typ]) or
                         (typ in ('integer', 'number') and isinstance(value, bool))):
        raise ValueError(path + ': tipo incorrecto')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path + ': valor no admitido')
    if isinstance(value, dict):
        props = schema.get('properties', {})
        missing = set(schema.get('required', [])) - value.keys()
        if missing:
            raise ValueError(path + ': faltan campos ' + ', '.join(sorted(missing)))
        for key, item in value.items():
            if key not in props:
                if schema.get('additionalProperties') is False:
                    raise ValueError(path + ': campo no admitido ' + key)
            else:
                validate_schema(root, props[key], item, path + '.' + key)
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            raise ValueError(path + ': lista vacia')
        for item in value:
            validate_schema(root, schema.get('items', {}), item, path + '[]')


def request_call(client, tool, operation, **fields):
    schema = request_schema(client, tool)
    props = schema.get('properties', {})
    if not operation_supported(client, tool, operation):
        raise RuntimeError(f'Operacion {operation} no disponible en {tool}')
    req = {'operation': operation}
    # Connect forbids connectionName. Never reuse the old model implicitly.
    if tool != 'connection_operations' or operation == 'GetConnection':
        if not client.connection_name:
            raise RuntimeError('No hay conexion valida; selecciona un modelo')
        if 'connectionName' not in props:
            raise RuntimeError('El esquema no permite identificar la conexion de forma explicita')
        req['connectionName'] = client.connection_name
    for key, value in fields.items():
        if value is None:
            continue
        if key not in props:
            raise RuntimeError(f'El servidor no admite el campo {key} para {tool}')
        req[key] = value
    args = {'request': req}
    enforce_policy(client, tool, args)
    validate_schema(tool_schema(client, tool), tool_schema(client, tool), args, 'arguments')
    try:
        return unwrap_result(client.tool(tool, args))
    except (RuntimeError, TimeoutError, OSError) as exc:
        invalidate_on_error(client, exc)
        raise


def invalidate_on_error(client, exc):
    message = str(exc).casefold()
    if isinstance(exc, (TimeoutError, OSError)) or any(word in message for word in (
            'connection', 'conexion', 'conexión', 'disconnected', 'servidor mcp se cerro', 'not connected')):
        client.connection_name = None
        client.model_name = None
        client.instance = None


def data_value(value):
    value = decode_json(value)
    for _ in range(12):
        if isinstance(value, dict) and 'data' in value:
            value = decode_json(value['data'])
        else:
            break
    return value


def object_rows(value, kind=None):
    value = data_value(value)
    if value is None:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(object_rows(item, kind))
        return result
    if isinstance(value, dict):
        for key in (kind, 'items', 'results', 'instances', 'rows'):
            if key and key in value:
                rows = object_rows(value[key], kind)
                if value.get('tableName') and key in ('measures', 'columns'):
                    rows = [dict(row, tableName=row.get('tableName') or value['tableName']) for row in rows]
                return rows
        if 'name' in value or 'port' in value or 'fromTable' in value:
            return [value]
        if not value:
            return []
    raise RuntimeError('Formato de respuesta desconocido; usa --debug u opcion 12')


def list_instances(client):
    return object_rows(request_call(client, 'connection_operations', 'ListLocalInstances'), 'instances')


def choose(items, prompt='Selecciona: ', multiple=False):
    raw = input(prompt).strip()
    if not raw or raw == '0':
        return [] if multiple else None
    try:
        indices = list(dict.fromkeys(int(v.strip()) - 1 for v in raw.split(',')))
        if not multiple and len(indices) != 1:
            raise ValueError()
        if any(i < 0 or i >= len(items) for i in indices):
            raise ValueError()
    except ValueError:
        raise ValueError('Seleccion invalida') from None
    selected = [items[i] for i in indices]
    return selected if multiple else selected[0]


def connect(client):
    print('\nBuscando modelos de Power BI Desktop...')
    instances = list_instances(client)
    if not instances:
        client.connection_name = client.model_name = client.instance = None
        print('No hay modelos abiertos. Abre Power BI Desktop y un archivo PBIX.')
        return
    print('Modelos encontrados:')
    for i, inst in enumerate(instances, 1):
        print(f'{i}. {inst.get("parentWindowTitle") or inst.get("name") or "Sin titulo"} (puerto {inst.get("port", "?")})')
    print('0. Cancelar')
    inst = choose(instances, 'Selecciona el modelo: ')
    if inst is None:
        return
    connstr = inst.get('connectionString')
    if not connstr:
        port = int(inst['port'])
        if not 1 <= port <= 65535:
            raise ValueError('Puerto invalido')
        connstr = f'Data Source=localhost:{port};Application Name=PBIModeling'
    # Clear old identity first: even an ambiguous Connect response must not select it.
    client.connection_name = client.model_name = client.instance = None
    result = request_call(client, 'connection_operations', 'Connect', connectionString=connstr)
    data = data_value(result)
    name = data if isinstance(data, str) else data.get('connectionName') if isinstance(data, dict) else None
    if not name and isinstance(result, dict):
        name = result.get('connectionName')
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError('El servidor no devolvio un identificador de conexion valido')
    client.connection_name = name
    client.model_name = inst.get('parentWindowTitle') or inst.get('name') or name
    client.instance = dict(inst)
    print('CONECTADO A:', client.model_name)


def ensure_connected(client):
    if not client.connection_name:
        print('Primero selecciona la opcion 1 para conectar.')
        return False
    try:
        if client.instance:
            instances = list_instances(client)
            if not any(str(i.get('port')) == str(client.instance.get('port')) and
                       (i.get('parentWindowTitle') or i.get('name')) ==
                       (client.instance.get('parentWindowTitle') or client.instance.get('name')) and
                       all(client.instance.get(key) is None or i.get(key) == client.instance[key]
                           for key in ('processId', 'startTime')) for i in instances):
                raise RuntimeError('Conexion cerrada: el modelo ya no aparece en Power BI Desktop')
        if operation_supported(client, 'connection_operations', 'GetConnection'):
            result = request_call(client, 'connection_operations', 'GetConnection')
            if data_value(result) is None:
                raise RuntimeError('Conexion invalida')
        return True
    except (RuntimeError, TimeoutError, OSError) as exc:
        client.connection_name = client.model_name = client.instance = None
        print('ERROR: conexion no disponible:', exc)
        return False


def list_objects(client, tool):
    fields = {}
    root = tool_schema(client, tool)
    props = request_schema(client, tool).get('properties', {})
    # Current server defaults to only 200 objects. Explicitly raise the cap and
    # warn at the cap rather than quietly claiming a complete model inventory.
    if 'filter' in props:
        filt = resolve_schema(root, props['filter']).get('properties', {})
        if 'maxResults' in filt:
            fields['filter'] = {'maxResults': 100000}
    rows = object_rows(request_call(client, tool, 'List', **fields), tool.split('_')[0] + 's')
    if len(rows) >= 100000:
        print('Advertencia: se alcanzo el limite; el listado puede estar incompleto.')
    return rows


def object_reference(obj, table=False):
    if not obj.get('name'):
        raise ValueError('El objeto no tiene nombre')
    ref = {'name': obj['name']}
    if not table:
        if not obj.get('tableName'):
            raise ValueError('El servidor no devolvio la tabla del objeto; no es seguro seleccionarlo')
        ref['tableName'] = obj['tableName']
    return ref


def get_object(client, tool, obj):
    rows = object_rows(request_call(client, tool, 'Get', references=[object_reference(obj, tool == 'table_operations')]), tool.split('_')[0] + 's')
    if len(rows) != 1:
        raise RuntimeError('No se pudo identificar un unico objeto')
    return dict(obj, **rows[0])


def show_objects(rows):
    for i, obj in enumerate(rows, 1):
        print(f'{i}. [{obj.get("name", "Sin nombre")}] — {obj.get("tableName", "")}')
    print('Total:', len(rows))


def display_result(result):
    data = data_value(result)
    if isinstance(data, list) and all(isinstance(x, dict) for x in data):
        display_grid(data)
    elif isinstance(data, dict):
        for key, value in data.items():
            print(f'{key}: {value}')
    else:
        print(data if data is not None else 'Sin resultados.')


def display_grid(rows):
    if not rows:
        print('Sin resultados.')
        return
    keys = list(dict.fromkeys(k for row in rows for k in row))
    widths = {k: min(60, max(len(str(k)), *(len(str(row.get(k, ''))) for row in rows))) for k in keys}
    print(' | '.join(str(k).ljust(widths[k]) for k in keys))
    for row in rows:
        print(' | '.join(str(row.get(k, ''))[:widths[k]].ljust(widths[k]) for k in keys))
    print('Filas:', len(rows), '(celdas en pantalla limitadas a 60 caracteres; CSV conserva el valor)')


def simple_list(client, tool, title):
    if not ensure_connected(client):
        return
    print(f'\n{title} | Modelo: {client.model_name}')
    rows = list_objects(client, tool)
    if tool == 'relationship_operations':
        for i, obj in enumerate(rows, 1):
            print(f'{i}. {obj.get("fromTable", "?")}[{obj.get("fromColumn", "?")}] → {obj.get("toTable", "?")}[{obj.get("toColumn", "?")}]')
            for key in ('fromCardinality', 'toCardinality', 'isActive', 'crossFilteringBehavior', 'securityFilteringBehavior'):
                if key in obj:
                    print(f'   {key}: {obj[key]}')
        print('Total:', len(rows))
        return
    show_objects(rows)
    if not rows:
        return
    if tool == 'measure_operations':
        while True:
            raw = input('Numero para ver DAX, /texto para filtrar nombre/tabla, Enter para regresar: ').strip()
            if not raw:
                return
            if raw.startswith('/'):
                text = raw[1:].casefold()
                filtered = [r for r in rows if text in (r.get('name', '') + ' ' + r.get('tableName', '')).casefold()]
                show_objects(filtered)
                if filtered:
                    obj = choose(filtered, 'Selecciona medida (Enter=regresar): ')
                else:
                    continue
            else:
                if not raw.isdigit() or not 1 <= int(raw) <= len(rows):
                    print('Seleccion invalida')
                    continue
                obj = rows[int(raw) - 1]
            if obj:
                show_details(get_object(client, tool, obj))
    elif operation_supported(client, tool, 'Get'):
        obj = choose(rows, 'Tabla para ver estructura (Enter=regresar): ')
        if obj:
            show_details(get_object(client, tool, obj))
            if 'column_operations' in client.tools:
                columns = [r for r in list_objects(client, 'column_operations') if r.get('tableName') == obj['name']]
                display_grid(columns)


def show_details(obj):
    for key in ('name', 'tableName', 'type', 'columnType', 'isCalculated', 'dataType', 'expression', 'description', 'displayFolder', 'formatString', 'isHidden'):
        if key in obj:
            print(f'{key}:\n{obj[key]}' if key == 'expression' else f'{key}: {obj[key]}')


def field_value(name, spec, required=False):
    typ = spec.get('type', 'string')
    if isinstance(typ, list):
        typ = next((t for t in typ if t != 'null'), 'string')
    if typ == 'object':
        return collect_fields(spec)
    if typ == 'array':
        print(f'  {name}: introduce lista JSON')
        raw = input('  JSON: ').strip()
        return json.loads(raw) if raw else None
    while True:
        value = input(f'  {name}{" *" if required else " (Enter=omitir)"}: ').strip()
        if not value:
            if required:
                print('  Campo obligatorio')
                continue
            return None
        if typ == 'boolean':
            if value.lower() in ('s', 'si', 'true', '1'):
                return True
            if value.lower() in ('n', 'no', 'false', '0'):
                return False
            print('  Escribe s o n')
            continue
        if typ in ('integer', 'number'):
            try:
                return int(value) if typ == 'integer' else float(value)
            except ValueError:
                print('  Numero invalido')
                continue
        return value


def collect_fields(schema, defaults=None, only_required=False):
    defaults = defaults or {}
    props = schema.get('properties', {})
    required = schema.get('required', [])
    result = dict(defaults)
    for name, spec in props.items():
        if name in result:
            continue
        if only_required and name not in required:
            continue
        value = field_value(name, spec, name in required)
        if value is not None:
            result[name] = value
    return result


def selected_operation_schema(client, tool):
    return request_schema(client, tool)


def multiline(label='Expresion DAX', allow_empty=False):
    print(label + ' (varias lineas; termina con FIN):')
    lines = []
    while True:
        line = input()
        if line.strip() == 'FIN':
            break
        lines.append(line)
    text = '\n'.join(lines).strip()
    if not text and not allow_empty:
        raise ValueError('La expresion no puede estar vacia')
    return text


def definition_properties(client, tool):
    root = tool_schema(client, tool)
    props = request_schema(client, tool).get('properties', {})
    if 'definitions' in props:
        spec = resolve_schema(root, props['definitions'])
        return resolve_schema(root, spec.get('items', {})).get('properties', {})
    return props


def build_object_fields(client, tool, operation, obj):
    props = request_schema(client, tool).get('properties', {})
    if operation in ('Create', 'Update'):
        available = definition_properties(client, tool)
        unsupported = set(obj) - set(available)
        if unsupported:
            raise RuntimeError('Propiedades no admitidas: ' + ', '.join(sorted(unsupported)))
        fields = {'definitions': [obj]} if 'definitions' in props else dict(obj)
    else:
        ref = object_reference(obj)
        if 'references' not in props:
            raise RuntimeError('Este esquema de referencias no es compatible; usa opcion 12')
        fields = {'references': [ref]}
        if 'shouldCascadeDelete' not in props:
            raise RuntimeError('El servidor no permite desactivar la eliminacion en cascada')
        fields['shouldCascadeDelete'] = False
    if 'options' in props:
        root = tool_schema(client, tool)
        supported = resolve_schema(root, props['options']).get('properties', {})
        options = {k: v for k, v in {'continueOnError': False, 'useTransaction': True}.items() if k in supported}
        if options:
            fields['options'] = options
    return fields


def confirmed_success(value):
    if isinstance(value, dict):
        if value.get('success') is True:
            return True
        if str(value.get('status', '')).casefold() in ('success', 'succeeded', 'completed'):
            return True
        message = str(value.get('message', '')).casefold()
        if 'successfully' in message and not any(x in message for x in ('not ', 'failed', 'pending')):
            return True
        return any(confirmed_success(v) for k, v in value.items() if k in ('data', 'results', 'items'))
    if isinstance(value, list):
        return bool(value) and all(confirmed_success(v) for v in value)
    return False


def verify_write(client, tool, operation, obj):
    """Confirm the resulting server state when a write returns empty content."""
    rows = list_objects(client, tool)
    present = any(row.get('name') == obj['name'] and row.get('tableName') == obj['tableName'] for row in rows)
    if operation == 'Delete':
        return not present
    if not present:
        return False
    current = get_object(client, tool, obj)
    for key, expected in obj.items():
        actual = current.get(key)
        if key == 'expression' and isinstance(actual, str):
            actual = actual.replace('\r\n', '\n').strip()
            expected = expected.replace('\r\n', '\n').strip()
        if expected == '' and actual is None and key in ('description', 'displayFolder', 'formatString'):
            continue
        if actual != expected:
            return False
    return True


def apply_objects(client, tool, operation, objects):
    if not client.writable:
        raise RuntimeError('Modo solo lectura. Reinicia: python cliente.py --write')
    if not objects:
        return []
    # Build and validate every request before asking for final confirmation.
    plans = [(obj, build_object_fields(client, tool, operation, obj)) for obj in objects]
    root = tool_schema(client, tool)
    for obj, fields in plans:
        args = {'request': dict(operation=operation, connectionName=client.connection_name, **fields)}
        validate_schema(root, root, args, 'arguments')
    print('\nVISTA PREVIA:', operation, '| Modelo:', client.model_name)
    for obj, _ in plans:
        show_details(obj)
    print('Los cambios pueden afectar dependencias y visuales. Guarda una copia del PBIX.')
    if operation == 'Delete':
        print('Se desactiva la eliminacion en cascada; si hay dependencias, el servidor puede rechazarla.')
    if input('Escribe CONFIRMAR para aplicar: ').strip() != 'CONFIRMAR':
        print('Cancelado')
        return []
    outcomes = []
    for obj, fields in plans:
        if not ensure_connected(client):
            print('Lote detenido. Los objetos restantes no se enviaron.')
            break
        label = f'{obj.get("tableName", "")}[{obj["name"]}]'
        try:
            result = request_call(client, tool, operation, **fields)
            explicit = confirmed_success(result)
            if explicit or verify_write(client, tool, operation, obj):
                print('CONFIRMADO' + ('' if explicit else ' POR LECTURA DEL MODELO') + ':', label)
                outcomes.append((label, 'confirmado'))
            else:
                print('SIN CONFIRMACION:', label, 'Consulta el modelo antes de volver a intentar.')
                display_result(result)
                outcomes.append((label, 'incierto'))
        except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
            print('ERROR:', label, exc)
            outcomes.append((label, 'error'))
            if not client.connection_name:
                print('Lote detenido. Verifica el modelo antes de reintentar.')
                break
    print('Resumen:', ', '.join(f'{label}: {state}' for label, state in outcomes) or 'Sin operaciones')
    return outcomes


def calculated_column(obj):
    # A calculated expression alone is insufficient: calculated-table columns
    # may also carry expressions but must not be treated as editable DAX columns.
    typ = str(obj.get('type', obj.get('columnType', ''))).casefold()
    if typ:
        return typ in ('calculated', 'calculatedcolumn')
    return obj.get('isCalculated') is True and not obj.get('sourceColumn')


def edit_objects(client, tool, operation, label):
    if not client.writable:
        print('Modo solo lectura. Reinicia: python cliente.py --write')
        return
    if not ensure_connected(client):
        return
    if not operation_supported(client, tool, operation):
        print('Operacion no disponible en esta version del servidor.')
        return
    print(f'\n{operation} {label} | Modelo: {client.model_name}')
    existing = list_objects(client, tool)
    objects = []
    available = definition_properties(client, tool)
    if operation == 'Create':
        tables = list_objects(client, 'table_operations')
        if not tables:
            print('No hay tablas disponibles.')
            return
        while True:
            show_objects(tables)
            table = choose(tables, 'Tabla (Enter=cancelar): ')
            if table is None:
                return
            name = input('Nombre: ').strip()
            if not name:
                raise ValueError('El nombre no puede estar vacio')
            # Measures have a model-wide namespace; columns have a table namespace.
            if any(r.get('name', '').casefold() == name.casefold() and
                   (tool == 'measure_operations' or r.get('tableName', '').casefold() == table['name'].casefold())
                   for r in existing + objects):
                raise ValueError('El nombre ya existe en el modelo o en este lote')
            obj = {'name': name, 'tableName': table['name'], 'expression': multiline()}
            if tool == 'column_operations':
                # Current schema infers calculated column from expression.
                if 'expression' not in available:
                    raise RuntimeError('El servidor no admite columnas calculadas con este esquema')
            objects.append(obj)
            if input('¿Agregar otro objeto? (s/N): ').strip().lower() != 's':
                break
    else:
        if tool == 'column_operations':
            tables = list_objects(client, 'table_operations')
            show_objects(tables)
            table = choose(tables, 'Tabla (Enter=cancelar): ')
            if not table:
                return
            existing = [r for r in existing if r.get('tableName') == table['name']]
            for obj in existing:
                print(f'{obj["name"]}: tipo {obj.get("type", obj.get("columnType", "no informado; se verificara al seleccionar"))}')
        show_objects(existing)
        selected = choose(existing, 'Selecciona uno o varios (1,3; Enter=cancelar): ', multiple=True)
        for obj in selected:
            current = get_object(client, tool, obj)
            show_details(current)
            if tool == 'column_operations' and not calculated_column(current):
                print('Omitida: columna importada, de tabla calculada o tipo no confirmado. Este menu solo edita/elimina columnas calculadas DAX.')
                continue
            if operation == 'Delete':
                objects.append(object_reference(current))
                continue
            changes = object_reference(current)
            expression = multiline('Nueva expresion DAX (FIN sin texto conserva la actual)', allow_empty=True)
            if expression and expression != current.get('expression'):
                changes['expression'] = expression
            for key, caption in (('description', 'Descripcion'), ('displayFolder', 'Carpeta de visualizacion'),
                                 ('formatString', 'Formato')):
                if key in available:
                    value = input(f'{caption} (Enter=conservar, /vaciar=borrar): ')
                    if value:
                        changes[key] = '' if value == '/vaciar' else value
            if len(changes) == 2:
                continue
            print('DIFERENCIAS:', current['name'])
            for key in changes.keys() - {'name', 'tableName'}:
                print(key + ':')
                for line in difflib.unified_diff(str(current.get(key, '')).splitlines(), str(changes[key]).splitlines(),
                                                 fromfile='Actual', tofile='Propuesto', lineterm=''):
                    print(line)
            objects.append(changes)
    apply_objects(client, tool, operation, objects)


def dax_rows(value):
    value = data_value(value)
    if isinstance(value, list):
        if all(isinstance(x, dict) for x in value):
            return value
    if isinstance(value, dict):
        if 'rows' in value:
            rows = value['rows']
            if isinstance(rows, list) and all(isinstance(x, dict) for x in rows):
                return rows
            columns = value.get('columns', [])
            names = [c.get('name', c.get('columnName', '')) if isinstance(c, dict) else str(c) for c in columns]
            if names and isinstance(rows, list) and all(isinstance(r, list) and len(r) == len(names) for r in rows):
                return [dict(zip(names, r)) for r in rows]
        for key in ('tables', 'results', 'result'):
            if key in value:
                nested = value[key]
                if not isinstance(nested, list):
                    nested = [nested]
                rows = []
                for item in nested:
                    rows.extend(dax_rows(item))
                return rows
    raise RuntimeError('Formato DAX no reconocido; consulta la respuesta en modo depuracion')


def save_csv(rows, path):
    path = Path(path)
    if path.exists():
        raise ValueError('El archivo ya existe; elige otro nombre')
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    print('CSV guardado:', path.resolve())


def dax_query(client):
    if not ensure_connected(client):
        return
    dax = multiline('Consulta DAX | Modelo: ' + client.model_name)
    # Only the query tool Execute operation is used; no XMLA/TMSL or model writes.
    props = request_schema(client, 'dax_query_operations').get('properties', {})
    key = next((k for k in ('query', 'daxQuery', 'queryText') if k in props), None)
    if not key:
        raise RuntimeError('Esquema DAX no compatible; consulta opcion 12')
    fields = {key: dax}
    if 'resultMode' in props:
        fields['resultMode'] = 'Inline'
    if 'maxRows' in props:
        fields['maxRows'] = 1000
    rows = dax_rows(request_call(client, 'dax_query_operations', 'Execute', **fields))
    display_grid(rows)
    if len(rows) >= 1000:
        print('Se alcanzo el limite de 1000 filas; el resultado puede estar truncado.')
    if rows:
        name = input('Guardar CSV (ruta; Enter=omitir): ').strip()
        if name:
            save_csv(rows, name)


def advanced(client):
    print('\nHerramientas disponibles:')
    for name in client.tools:
        print('-', name)
    name = input('Herramienta (Enter=cancelar): ').strip()
    if not name:
        return
    schema = tool_schema(client, name)
    pretty(schema)
    if input('Ejecutar manualmente? (s/N): ').lower() != 's':
        return
    if not client.writable:
        print('Modo solo lectura: operaciones de escritura bloqueadas por el cliente y servidor.')
    print('Pega un objeto JSON completo y escribe FIN en una linea aparte:')
    lines = []
    while True:
        line = input()
        if line.strip() == 'FIN':
            break
        lines.append(line)
    args = json.loads('\n'.join(lines))
    if not isinstance(args, dict):
        raise ValueError('Se requiere un objeto JSON')
    request = args.get('request')
    if not isinstance(request, dict):
        raise ValueError('Se requiere un objeto request')
    if name != 'connection_operations' and request.get('operation') != 'Help':
        if not ensure_connected(client):
            return
        props = request_schema(client, name).get('properties', {})
        if 'connectionName' not in props:
            raise RuntimeError('Esta herramienta no permite indicar la conexion explicitamente')
        if request.get('connectionName') not in (None, client.connection_name):
            raise RuntimeError('La conexion indicada no coincide con el modelo activo')
        request['connectionName'] = client.connection_name
    enforce_policy(client, name, args)
    if args.get('request', {}).get('operation') == 'Delete':
        print('Posibles dependencias afectadas. Guarda una copia del PBIX.')
        if input('Escribe CONFIRMAR para eliminar: ').strip() != 'CONFIRMAR':
            return
    elif input('Ejecutar? (s/N): ').strip().lower() != 's':
        return
    validate_schema(schema, schema, args, 'arguments')
    try:
        pretty(client.tool(name, args))
    finally:
        if name == 'connection_operations' and args.get('request', {}).get('operation') not in ('Help', 'ListConnections', 'ListLocalInstances', 'GetConnection'):
            client.connection_name = client.model_name = client.instance = None
            print('Selecciona de nuevo el modelo desde la opcion 1 para sincronizar el menu.')


def main():
    parser = argparse.ArgumentParser(description='Cliente de consola Power BI MCP (biblioteca estandar)')
    parser.add_argument('--write', action='store_true', help='Habilitar edicion')
    parser.add_argument('--debug', action='store_true', help='Mostrar respuestas MCP completas')
    parser.add_argument('--server', type=Path, help='Ejecutable autorizado (opcional)')
    opts = parser.parse_args()
    if opts.write:
        print('MODO EDICION. Haz copia del PBIX antes de modificarlo.')
        if input('Escribe EDITAR para continuar: ').strip() != 'EDITAR':
            return
    else:
        print('SOLO LECTURA. Para editar: python cliente.py --write')
    client = MCP(opts.write, opts.server, opts.debug)
    try:
        try:
            connect(client)
        except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
            print('ERROR:', exc)
        while True:
            print('\n========== POWER BI MCP ==========')
            print('Modelo:', client.model_name or 'SIN CONEXION')
            print('Estado:', 'CONECTADO (ultima verificacion)' if client.connection_name else 'SIN CONEXION')
            print('Modo:', 'EDICION' if client.writable else 'LECTURA')
            print('1. Conectar / cambiar modelo')
            print('2. Listar tablas')
            print('3. Listar medidas')
            print('4. Crear medidas')
            print('5. Modificar medidas')
            print('6. Eliminar medidas')
            print('7. Crear columnas')
            print('8. Modificar columnas')
            print('9. Eliminar columnas')
            print('10. Consultar relaciones')
            print('11. Ejecutar consulta DAX')
            print('12. Herramienta avanzada')
            print('0. Salir')
            choice = input('Opcion: ').strip()
            if choice == '0':
                break
            try:
                if choice == '1':
                    connect(client)
                elif choice == '2':
                    simple_list(client, 'table_operations', 'TABLAS')
                elif choice == '3':
                    simple_list(client, 'measure_operations', 'MEDIDAS')
                elif choice in ('4', '5', '6'):
                    operation = {'4': 'Create', '5': 'Update', '6': 'Delete'}[choice]
                    edit_objects(client, 'measure_operations', operation, 'MEDIDAS')
                elif choice in ('7', '8', '9'):
                    operation = {'7': 'Create', '8': 'Update', '9': 'Delete'}[choice]
                    edit_objects(client, 'column_operations', operation, 'COLUMNAS')
                elif choice == '10':
                    simple_list(client, 'relationship_operations', 'RELACIONES')
                elif choice == '11':
                    dax_query(client)
                elif choice == '12':
                    advanced(client)
                else:
                    print('Opcion invalida')
            except (ValueError, RuntimeError, TimeoutError, KeyError, TypeError, OSError) as exc:
                print('ERROR:', exc)
                invalidate_on_error(client, exc)
            if choice != '0':
                input('\nEnter para volver al menu...')
    finally:
        client.close()

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nSesion finalizada.')
    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
        print('ERROR:', exc)
        sys.exit(1)
