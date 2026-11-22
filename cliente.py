import argparse
from datetime import datetime, timezone
import csv
import difflib
import json
import os
import uuid
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
        self.server_path = server
        self.mcp_session_token = uuid.uuid4().hex
        self.target_identity = None
        self.last_connection_name = None
        self.engine_session_id = None
        self.connection_issue = None
        self.active_transaction = None
        self.retired_mcp_sessions = []
        self.connection_events = []
        args = [str(server), '--start', '--readwrite' if writable else '--readonly']
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding='utf-8',
                                        errors='replace', bufsize=1)
        self.inbox = queue.Queue()
        self.counter = 0
        self.connection_name = None
        self.instance = None
        self.model_name = None
        self._reader_thread = threading.Thread(target=self._read, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()
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
            if getattr(self, 'writable', False) and getattr(self, '_auto_write_scope', False):
                props = schema.get('properties', {})
                sensitive = re.search(r'eula|licen[cs]|password|credential|token|contrase',
                                      str(params.get('message', '')) + ' ' + ' '.join(props), re.IGNORECASE)
                consent_fields = all(re.search(r'allow|approv|accept|confirm|consent|proceed|authoriz|permit|autoriz', key, re.IGNORECASE) for key in props)
                if props and consent_fields and all(spec.get('type') == 'boolean' for spec in props.values()) and not sensitive:
                    content = {key: True for key in props}
                    validate_schema(schema, schema, content, 'autorizacion --write')
                    result = {'action': 'accept', 'content': content}
                    print('Consentimiento de escritura del servidor autorizado por --write para este bloque.')
                else:
                    result = {'action': 'decline'}
                    print('Solicitud adicional del servidor no compatible con autorizacion automatica; se rechaza sin pedir datos.')
                self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
                return
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
        for thread in (getattr(self, '_reader_thread', None), getattr(self, '_stderr_thread', None)):
            if thread:
                thread.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                stream.close()

    def restart_transport(self):
        """Restart a dead MCP process, keeping the selected target, not its session."""
        if self.process.poll() is None:
            raise RuntimeError('No se reinicia un proceso MCP vivo con solicitudes posiblemente pendientes')
        retired = list(self.retired_mcp_sessions) + [self.mcp_session_token]
        keep = {name: getattr(self, name, None) for name in (
            'target_identity', 'instance', 'model_name', 'last_connection_name',
            'backup_dir', 'last_backup_path', 'last_recovery', '_block_active', '_auto_write_scope', 'connection_events')}
        writable, server, debug = self.writable, self.server_path, self.debug
        self.close()
        if any(thread and thread.is_alive() for thread in (self._reader_thread, self._stderr_thread)):
            raise RuntimeError('Los lectores de la sesion MCP anterior no finalizaron; reconexion detenida')
        self.__init__(writable=writable, server=server, debug=debug)
        self.retired_mcp_sessions = retired
        for name, value in keep.items():
            if value is not None:
                setattr(self, name, value)


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
    lost = re.search(r'(?:connection|conexi[oó]n).{0,100}(?:not found|does not exist|closed|lost|invalid|perdida|cerrada|invalida)|'
                     r'not connected|disconnected|servidor mcp se cerro|proceso mcp termino', message)
    if isinstance(exc, (TimeoutError, OSError, ConnectionIdentityError)) or lost:
        if client.connection_name:
            client.last_connection_name = client.connection_name
        if not getattr(client, 'target_identity', None) and getattr(client, 'instance', None):
            client.target_identity = {'instance': dict(client.instance), 'database': None}
        client.connection_name = None
        client.connection_issue = str(exc)


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


class ConnectionIdentityError(RuntimeError):
    pass


def normalized_port(value):
    try:
        port = int(value)
        return port if 1 <= port <= 65535 else None
    except (ValueError, TypeError):
        return None


def normalized_start(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else stamp.isoformat()
    except ValueError:
        return None


def same_instance(expected, observed):
    if normalized_port(expected.get('port')) != normalized_port(observed.get('port')):
        return False
    for key in ('processId',):
        if expected.get(key) is not None and observed.get(key) is not None:
            if str(expected[key]).strip() != str(observed[key]).strip():
                return False
    a, b = normalized_start(expected.get('startTime')), normalized_start(observed.get('startTime'))
    if a and b:
        # Compare only comparable aware/naive timestamps; database identity remains
        # mandatory for reconnection if a server omits or changes the time format.
        aware_a, aware_b = '+' in a[10:] or a.endswith('Z'), '+' in b[10:] or b.endswith('Z')
        if aware_a == aware_b and a != b:
            return False
    return True


def connection_details(client, name):
    if not operation_supported(client, 'connection_operations', 'GetConnection'):
        return {}
    result = request_call(client, 'connection_operations', 'GetConnection', connectionName=name)
    data = data_value(result)
    if not isinstance(data, dict):
        raise RuntimeError('GetConnection no devolvio detalles validos de la conexion')
    return data


def check_connection_database(details, identity):
    database = details.get('databaseId') or details.get('databaseName')
    expected = identity.get('database')
    if expected and (not database or str(database).casefold() != str(expected).casefold()):
        raise ConnectionIdentityError('La base de datos no coincide con el modelo seleccionado; se rechaza la conexion')
    server = details.get('serverName')
    if server:
        match = re.fullmatch(r'(?:localhost|127\.0\.0\.1|\[::1\]|\.|\(local\))[:,](\d+)', str(server).strip(), re.IGNORECASE)
        if not match or normalized_port(match.group(1)) != normalized_port(identity['instance'].get('port')):
            raise ConnectionIdentityError('El servidor/puerto de GetConnection no coincide con la instancia seleccionada')
    if details.get('isConnected') is False:
        raise RuntimeError('La conexion del servidor esta desconectada')
    return database


def attach_instance(client, instance, expected=None):
    port = normalized_port(instance.get('port'))
    if not port:
        raise ConnectionIdentityError('Puerto de Power BI no valido')
    # Reconnection is always local, on the exact selected port. No title search,
    # alternative port, default model or connection string supplied in an AI block.
    connstr = (f'Data Source=localhost:{port};Application Name=PBIModeling' if expected
               else instance.get('connectionString') or f'Data Source=localhost:{port};Application Name=PBIModeling')
    result = request_call(client, 'connection_operations', 'Connect', connectionString=connstr)
    data = data_value(result)
    name = data if isinstance(data, str) else data.get('connectionName') if isinstance(data, dict) else None
    if not name and isinstance(result, dict):
        name = result.get('connectionName')
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError('El servidor no devolvio un identificador de conexion valido')
    client.connection_name = name
    identity = expected or {'instance': dict(instance), 'database': None}
    try:
        details = connection_details(client, name)
        database = check_connection_database(details, identity)
    except (RuntimeError, TimeoutError, OSError):
        client.connection_name = None
        raise
    if not expected:
        identity['database'] = database
        client.target_identity = identity
    client.instance = dict(instance)
    client.model_name = instance.get('parentWindowTitle') or instance.get('name') or name
    client.last_connection_name = name
    client.engine_session_id = details.get('sessionId')
    client.connection_issue = None
    return name


def connect(client):
    print('\nBuscando modelos de Power BI Desktop...')
    instances = list_instances(client)
    if not instances:
        client.connection_name = client.model_name = client.instance = None
        client.target_identity = None
        print('No hay modelos abiertos. Abre Power BI Desktop y un archivo PBIX.')
        return
    print('Modelos encontrados:')
    for i, inst in enumerate(instances, 1):
        print(f'{i}. {inst.get("parentWindowTitle") or inst.get("name") or "Sin titulo"} (puerto {inst.get("port", "?")})')
    print('0. Cancelar')
    inst = choose(instances, 'Selecciona el modelo: ')
    if inst is None:
        return
    client.connection_name = client.model_name = client.instance = None
    client.target_identity = None
    client.last_connection_name = None
    attach_instance(client, inst)
    print('CONECTADO A:', client.model_name)


def selected_target(client):
    identity = getattr(client, 'target_identity', None)
    if identity:
        return identity
    if client.instance:
        identity = {'instance': dict(client.instance), 'database': None}
        client.target_identity = identity
        return identity
    return None


def same_target(a, b):
    if not a or not b:
        return a == b
    if not same_instance(a['instance'], b['instance']):
        return False
    return not a.get('database') or (b.get('database') and str(a['database']).casefold() == str(b['database']).casefold())


def ensure_connected(client, allow_reconnect=True, allow_restart=True):
    identity = selected_target(client)
    if not client.connection_name and not identity:
        print('Primero selecciona la opcion 1 para conectar.')
        return False
    issue = None
    try:
        process = getattr(client, 'process', None)
        if process is not None and process.poll() is not None:
            if not allow_restart or not allow_reconnect:
                raise RuntimeError('El proceso MCP termino; la sesion original ya no esta disponible')
            if not identity or not identity.get('database'):
                raise ConnectionIdentityError('No hay identidad de base de datos para reiniciar y reconectar con seguridad')
            client.restart_transport()
            issue = 'El proceso MCP termino; se inicio una nueva sesion para el mismo destino'
        observed = None
        if identity:
            instances = list_instances(client)
            candidates = [item for item in instances if normalized_port(item.get('port')) == normalized_port(identity['instance'].get('port'))]
            if len(candidates) != 1 or not same_instance(identity['instance'], candidates[0]):
                raise ConnectionIdentityError('La instancia seleccionada ya no aparece con el mismo puerto y proceso. No se buscara otro modelo por titulo')
            observed = candidates[0]
        if client.connection_name:
            try:
                details = connection_details(client, client.connection_name)
                if identity:
                    database = check_connection_database(details, identity)
                    if not identity.get('database') and database:
                        identity['database'] = database
                if getattr(client, 'active_transaction', None) and getattr(client, 'engine_session_id', None) and details.get('sessionId') != client.engine_session_id:
                    raise ConnectionIdentityError('Cambio la sesion del motor durante una transaccion; no se continuara escribiendo')
                client.last_connection_name = client.connection_name
                client.engine_session_id = details.get('sessionId') or getattr(client, 'engine_session_id', None)
                if observed:
                    client.instance = dict(observed)
                    client.model_name = observed.get('parentWindowTitle') or client.model_name
                client.connection_issue = None
                return True
            except ConnectionIdentityError:
                raise
            except (RuntimeError, TimeoutError, OSError) as exc:
                issue = str(exc)
        if not allow_reconnect:
            raise RuntimeError('No se puede reutilizar la sesion de la transaccion: ' + str(issue or 'conexion perdida'))
        if not identity or not identity.get('database') or not observed:
            raise ConnectionIdentityError('No hay identidad suficiente para reconectar automaticamente; selecciona el modelo de nuevo')
        previous = client.connection_name or getattr(client, 'last_connection_name', None)
        client.connection_name = None
        attach_instance(client, observed, expected=identity)
        client.reconnection_count = getattr(client, 'reconnection_count', 0) + 1
        if not hasattr(client, 'connection_events'):
            client.connection_events = []
        client.connection_events.append({'event': 'reconexion_verificada', 'reason': issue or 'alias no disponible',
            'port': normalized_port(observed['port']), 'database': identity['database'],
            'previousAlias': previous, 'currentAlias': client.connection_name,
            'mcpSession': getattr(client, 'mcp_session_token', None), 'engineSession': getattr(client, 'engine_session_id', None)})
        client.connection_issue = None
        print(f'RECONEXION VERIFICADA: mismo modelo, base de datos y puerto {observed["port"]}.')
        if getattr(client, 'debug', False):
            print('Diagnostico de reconexion:', issue or 'identificador de conexion perdido', '| alias anterior:', previous, '| alias actual:', client.connection_name)
        return True
    except (RuntimeError, TimeoutError, OSError) as exc:
        client.connection_issue = str(exc)
        if not hasattr(client, 'connection_events'):
            client.connection_events = []
        client.connection_events.append({'event': 'conexion_rechazada', 'reason': str(exc),
                                          'mcpSession': getattr(client, 'mcp_session_token', None)})
        client.connection_name = None
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
    rows = object_rows(request_call(client, tool, 'Get', references=[object_reference(obj, tool in ('table_operations', 'relationship_operations'))]), tool.split('_')[0] + 's')
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


def metadata_export(client, section='all'):
    """Keep every property returned by Get, including formatting and annotations."""
    result = {'schemaVersion': 1, 'model': client.model_name, 'complete': True,
              'warnings': ['Metadatos del modelo; no incluye datos de filas ni todos los usos en visuales/filtros del informe.'],
              'errors': []}
    mapping = {'tables': 'table_operations', 'measures': 'measure_operations',
               'columns': 'column_operations', 'relationships': 'relationship_operations'}
    sections = mapping if section == 'all' else {section: mapping[section]}
    for label, tool in sections.items():
        result[label] = []
        try:
            rows = list_objects(client, tool)
            if len(rows) >= 100000:
                raise RuntimeError('Inventario posiblemente truncado')
            for row in rows:
                try:
                    if tool == 'relationship_operations':
                        details = object_rows(request_call(client, tool, 'Get', references=[{'name': row['name']}]), 'relationships')
                        if len(details) != 1:
                            raise RuntimeError('No se pudo leer una relacion unica')
                        detail = dict(row, **details[0])
                    else:
                        detail = get_object(client, tool, row)
                    result[label].append(detail)
                except (RuntimeError, ValueError, TimeoutError, OSError, KeyError) as exc:
                    result[label].append(dict(row, metadataIncomplete=True))
                    result['errors'].append(f'{label}: {row.get("name")}: {exc}')
        except (RuntimeError, ValueError, TimeoutError, OSError, KeyError) as exc:
            result['errors'].append(f'{label}: {exc}')
    if 'columns' in result:
        result['calculatedColumns'] = [dict(col) for col in result['columns'] if calculated_column(col)]
    result['complete'] = not result['errors']
    return result


def print_metadata_export(client, section='all'):
    data = metadata_export(client, section)
    folder = Path(getattr(client, 'export_dir', Path.cwd() / 'exports'))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('metadatos_' + section + '_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8] + '.json')
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text, encoding='utf-8')
    print('\n========== JSON DE METADATOS ==========')
    print(text)
    print('========== FIN JSON ==========')
    print('Archivo UTF-8:', path.resolve())
    if not data['complete']:
        print('AVISO: exportacion parcial; consulta errors antes de usarla.')
    return data


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
    while True:
        section = 'measures' if tool == 'measure_operations' else 'all'
        raw = input('Numero para ver detalle, json=exportar JSON completo, /texto para filtrar, Enter para regresar: ').strip()
        if not raw:
            return
        if raw.casefold() == 'json':
            print_metadata_export(client, section)
            continue
        if raw.startswith('/'):
            text = raw[1:].casefold()
            filtered = [r for r in rows if text in (r.get('name', '') + ' ' + r.get('tableName', '')).casefold()]
            show_objects(filtered)
            obj = choose(filtered, 'Selecciona objeto (Enter=regresar): ') if filtered else None
        elif raw.isdigit() and 1 <= int(raw) <= len(rows):
            obj = rows[int(raw) - 1]
        else:
            print('Seleccion invalida')
            continue
        if obj:
            show_details(get_object(client, tool, obj))
            if tool == 'table_operations' and 'column_operations' in client.tools:
                display_grid([r for r in list_objects(client, 'column_operations') if r.get('tableName') == obj['name']])


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


AI_ACTIONS = {
    'create_measure': ('measure_operations', 'Create'),
    'update_measure': ('measure_operations', 'Update'),
    'delete_measure': ('measure_operations', 'Delete'),
    'create_column': ('column_operations', 'Create'),
    'update_column': ('column_operations', 'Update'),
    'delete_column': ('column_operations', 'Delete'),
    'execute_dax': ('dax_query_operations', 'Execute'),
}
AI_PROPERTIES = {'description', 'displayFolder', 'formatString', 'isHidden', 'dataType'}
AI_LABELS = {
    'create_measure': 'Crear medida', 'update_measure': 'Modificar medida',
    'delete_measure': 'Eliminar medida', 'create_column': 'Crear columna calculada',
    'update_column': 'Modificar columna calculada', 'delete_column': 'Eliminar columna calculada',
    'execute_dax': 'Consultar DAX',
}


def parse_ai_block(text):
    """Strict JSON only. No code evaluation, shell commands or connection overrides."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Clave JSON duplicada: ' + key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError('Constante JSON no permitida: ' + value)

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('El bloque debe ser un objeto JSON')
    if 'operations' in value:
        if set(value) != {'operations'} or not isinstance(value['operations'], list):
            raise ValueError('Usa un objeto con solo operations y una lista de operaciones')
        operations = value['operations']
    else:
        operations = [value]
    if not operations:
        raise ValueError('El bloque no contiene operaciones')
    expanded = []
    for index, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            raise ValueError(f'Instruccion {index}: se requiere un objeto')
        if op.get('action') == 'delete_measures':
            if set(op) - {'action', 'table', 'names', 'force'} or not {'action', 'table', 'names'} <= op.keys() or not isinstance(op['names'], list) or not op['names']:
                raise ValueError(f'Instruccion {index}: delete_measures requiere table y names no vacio')
            expanded.extend(dict(action='delete_measure', table=op['table'], name=name,
                                 **({'force': op['force']} if 'force' in op else {})) for name in op['names'])
        else:
            expanded.append(dict(op))
    for index, op in enumerate(expanded, 1):
        action = op.get('action')
        if not isinstance(action, str) or action not in AI_ACTIONS:
            raise ValueError(f'Operacion {index}: accion no permitida')
        if action == 'execute_dax':
            allowed, required = {'action', 'query', 'maxRows'}, {'action', 'query'}
        else:
            required = {'action', 'table', 'name'}
            allowed = required | {'force'} if action.startswith('delete_') else required | AI_PROPERTIES | {'expression'}
            if action.startswith('create_'):
                required = required | {'expression'}
        if set(op) - allowed or required - set(op):
            raise ValueError(f'Operacion {index}: campos faltantes o no permitidos: ' +
                             ', '.join(sorted((set(op) - allowed) | (required - set(op)))))
        for key in ('table', 'name', 'expression', 'query'):
            if key in op and (not isinstance(op[key], str) or not op[key].strip()):
                raise ValueError(f'Operacion {index}: {key} debe ser texto no vacio')
        for key in AI_PROPERTIES & op.keys():
            if (key == 'isHidden' and not isinstance(op[key], bool)) or (key != 'isHidden' and not isinstance(op[key], str)):
                raise ValueError(f'Operacion {index}: tipo incorrecto para {key}')
        if action.startswith('update_') and not (op.keys() & (AI_PROPERTIES | {'expression'})):
            raise ValueError(f'Operacion {index}: no hay propiedades para modificar')
        if 'maxRows' in op and (type(op['maxRows']) is not int or not 1 <= op['maxRows'] <= 100000):
            raise ValueError(f'Operacion {index}: maxRows debe estar entre 1 y 100000')
        if 'force' in op and type(op['force']) is not bool:
            raise ValueError(f'Operacion {index}: force debe ser true o false')
    return expanded


def dax_references(expression):
    """Conservative lexical scan, not a full DAX parser; ignores strings/comments."""
    pattern = re.compile(r'"(?:[^"]|"")*"|//[^\n]*|--[^\n]*|/\*.*?\*/|'
                         r"(?:(?:'((?:[^']|'')+)'|([\w]+))\s*)?\[((?:[^\]]|\]\])+?)\]", re.DOTALL)
    refs = set()
    keywords = {'return', 'var', 'not', 'in', 'and', 'or', 'evaluate', 'define',
                'measure', 'column', 'table', 'by', 'asc', 'desc', 'true', 'false'}
    for match in pattern.finditer(expression):
        if match.group(3) is not None:
            table = match.group(1) or match.group(2)
            # RETURN [Measure] is not an unquoted table called RETURN. A quoted
            # table with that name ('RETURN'[Column]) remains a qualified reference.
            if match.group(1) is None and table and table.casefold() in keywords:
                table = None
            refs.add((table.replace("''", "'").casefold() if table else None,
                      match.group(3).replace(']]', ']').casefold()))
    return refs


def ai_object(op):
    return {'tableName': op['table'], 'name': op['name'],
            **{key: op[key] for key in AI_PROPERTIES | {'expression'} if key in op}}


def ai_query_fields(client, op):
    props = request_schema(client, 'dax_query_operations').get('properties', {})
    fields = {'query': op['query']}
    if 'resultMode' in props:
        fields['resultMode'] = 'Inline'
    if 'maxRows' in props:
        fields['maxRows'] = op.get('maxRows', 1000)
    elif 'maxRows' in op:
        raise ValueError('El servidor no admite maxRows')
    return fields


def validate_ai_block(client, operations):
    """Preflight complete ordered plan against a virtual, case-insensitive catalog."""
    if not client.connection_name:
        raise ValueError('No hay conexion activa')
    if not client.writable and any(op['action'] != 'execute_dax' for op in operations):
        raise ValueError('El bloque contiene escrituras: inicia con --write')
    tables = {row['name'].casefold(): row['name'] for row in list_objects(client, 'table_operations')}
    kinds = {AI_ACTIONS[op['action']][0] for op in operations} - {'dax_query_operations'}
    # Measures are also needed to resolve DAX references in column expressions.
    if any('expression' in op for op in operations) or any(op['action'].startswith('delete_') for op in operations):
        kinds.add('measure_operations')
    inventory = {tool: {} for tool in kinds}
    for tool in kinds:
        for row in list_objects(client, tool):
            if not row.get('tableName'):
                raise ValueError('No se pudo identificar la tabla de un objeto')
            inventory[tool][(row['tableName'].casefold(), row['name'].casefold())] = dict(row)
    plan, warnings, seen, histories, removed = [], [], set(), {}, set()
    relationships = list_objects(client, 'relationship_operations') if any(op['action'] == 'delete_column' for op in operations) else []
    measure_tool = 'measure_operations'
    # Read metadata once per tool, not once per instruction in a large block.
    if any(op['action'].startswith('delete_') or 'expression' in op for op in operations):
        for tool in ('measure_operations', 'column_operations'):
            if tool not in client.tools or not operation_supported(client, tool, 'Get'):
                warnings.append(f'{tool}: no se pueden leer todas las dependencias en esta version del servidor.')
                continue
            if tool not in inventory:
                inventory[tool] = {(r['tableName'].casefold(), r['name'].casefold()): dict(r)
                                   for r in list_objects(client, tool)}
            rows = list(inventory[tool].values())
            if rows:
                details = object_rows(request_call(client, tool, 'Get', references=[object_reference(r) for r in rows]),
                                      'measures' if tool == measure_tool else 'columns')
                by_name = {}
                for key in inventory[tool]:
                    by_name.setdefault(key[1], []).append(key)
                for detail in details:
                    keys = [key for key in by_name.get(str(detail.get('name', '')).casefold(), []) if
                            (not detail.get('tableName') or key[0] == detail['tableName'].casefold())]
                    if len(keys) == 1:
                        inventory[tool][keys[0]].update(detail)
                if any('expression' not in r and tool == measure_tool for r in inventory[tool].values()):
                    warnings.append('Algunas expresiones existentes no se pudieron leer; el analisis de dependencias es parcial.')
                if tool == 'column_operations' and any('columnType' not in r for r in inventory[tool].values()):
                    warnings.append('No se pudieron asociar todos los detalles de columnas; el analisis de dependencias es parcial.')
    future = {(op['table'].casefold(), op['name'].casefold()): i for i, op in enumerate(operations, 1)
              if op['action'].startswith('create_')}
    for index, original in enumerate(operations, 1):
        op = dict(original)
        tool, operation = AI_ACTIONS[op['action']]
        if not operation_supported(client, tool, operation):
            raise ValueError(f'Operacion {index}: {tool}.{operation} no disponible')
        fingerprint = json.dumps(op, sort_keys=True, ensure_ascii=False)
        if fingerprint in seen:
            raise ValueError(f'Operacion {index}: instruccion duplicada')
        seen.add(fingerprint)
        if tool == 'dax_query_operations':
            for ref_table, ref_name in dax_references(op['query']):
                if any(n == ref_name and (ref_table is None or t == ref_table) for t, n in removed):
                    raise ValueError(f'Operacion {index}: consulta utiliza un objeto eliminado previamente: [{ref_name}]')
                if any(n == ref_name and (ref_table is None or t == ref_table) for t, n in future) and not any(
                        k[1] == ref_name and (ref_table is None or k[0] == ref_table)
                        for rows in inventory.values() for k in rows):
                    raise ValueError(f'Operacion {index}: consulta utiliza un objeto creado despues: [{ref_name}]')
            fields = ai_query_fields(client, op)
        else:
            key = (op['table'].casefold(), op['name'].casefold())
            if key[0] not in tables:
                raise ValueError(f'Operacion {index}: tabla inexistente: {op["table"]}')
            op['table'] = tables[key[0]]
            objects = inventory[tool]
            current = objects.get(key)
            if operation == 'Create':
                conflict = current or (tool == measure_tool and any(k[1] == key[1] for k in objects))
                if conflict:
                    raise ValueError(f'Operacion {index}: nombre ya existente: {op["name"]}')
            elif not current:
                raise ValueError(f'Operacion {index}: objeto inexistente: {op["table"]}[{op["name"]}]')
            if current:
                op['name'] = current['name']
            target = (tool, key)
            history = histories.setdefault(target, [])
            if operation in history:
                raise ValueError(f'Operacion {index}: accion repetida para el mismo objeto')
            if history and history[-1] == 'Delete':
                raise ValueError(f'Operacion {index}: recreacion contradictoria tras Delete; divide los bloques')
            if history:
                warnings.append(f'Operacion {index}: {operation} actua sobre un objeto utilizado antes en el lote.')
            if tool == 'column_operations' and current and not history:
                current.update(get_object(client, tool, current))
                if not calculated_column(current):
                    raise ValueError(f'Operacion {index}: columna de origen o tipo no compatible')
            if operation == 'Delete':
                forced = op.get('force', False)
                if tool == 'column_operations' and any(
                        (str(r.get(side + 'Table', '')).casefold(), str(r.get(side + 'Column', '')).casefold()) == key
                        for r in relationships for side in ('from', 'to')):
                    if not forced:
                        raise ValueError(f'Operacion {index}: la columna participa en una relacion existente')
                    warnings.append(f'Operacion {index}: FORZADA; la columna participa en una relacion y la cascada puede eliminarla.')
                for other_tool, rows in inventory.items():
                    for other_key, other in rows.items():
                        if other_tool == tool and other_key == key:
                            continue
                        refs = dax_references(other.get('expression') or '')
                        if any(name == key[1] and (table is None or table == key[0]) for table, name in refs):
                            if not forced:
                                raise ValueError(f'Operacion {index}: posible dependencia de {other.get("tableName", "?")}[{other["name"]}]; modifica o elimina primero ese objeto')
                            warnings.append(f'Operacion {index}: FORZADA; puede afectar {other.get("tableName", "?")}[{other["name"]}].')
                del objects[key]
                removed.add(key)
            else:
                for ref_table, ref_name in dax_references(op.get('expression', '')):
                    if ref_name == key[1] and (ref_table is None or ref_table == key[0]):
                        raise ValueError(f'Operacion {index}: posible referencia circular al propio objeto')
                    if any(n == ref_name and (ref_table is None or t == ref_table) for t, n in removed):
                        raise ValueError(f'Operacion {index}: referencia a objeto eliminado previamente: [{ref_name}]')
                    candidates = [(t, n) for t, n in future if n == ref_name and (ref_table is None or t == ref_table)]
                    known = any(k[1] == ref_name and (ref_table is None or k[0] == ref_table)
                                for rows in inventory.values() for k in rows)
                    if candidates and not known:
                        raise ValueError(f'Operacion {index}: referencia a objeto creado despues o a si mismo: [{ref_name}]')
                    if known:
                        warnings.append(f'Operacion {index}: posible dependencia DAX de [{ref_name}]; se conserva el orden indicado.')
                objects[key] = dict(current or {}, **ai_object(op))
                if tool == 'column_operations' and operation == 'Create':
                    objects[key]['columnType'] = 'Calculated'
            history.append(operation)
            fields = build_object_fields(client, tool, operation, ai_object(op))
            if operation == 'Delete' and op.get('force'):
                fields['shouldCascadeDelete'] = True
        root = tool_schema(client, tool)
        req = {'request': dict(operation=operation, connectionName=client.connection_name, **fields)}
        unknown = set(fields) - request_schema(client, tool).get('properties', {}).keys()
        if unknown:
            raise ValueError(f'Operacion {index}: campos no admitidos por el servidor: {sorted(unknown)}')
        validate_schema(root, root, req, f'Operacion {index}')
        plan.append({'instruction': op, 'tool': tool, 'operation': operation, 'fields': fields})
    # Detect indirect cycles among the measure definitions available to preflight.
    measures = inventory.get(measure_tool, {})
    names = {}
    for key in measures:
        names.setdefault(key[1], set()).add(key)
    graph = {}
    for key, obj in measures.items():
        refs = dax_references(obj.get('expression') or '')
        graph[key] = {other for table, name in refs for other in names.get(name, ())
                      if table is None or table == other[0]}
    touched = {(item['instruction']['table'].casefold(), item['instruction']['name'].casefold())
               for item in plan if item['tool'] == measure_tool and item['operation'] != 'Delete'}
    # Iterative traversal avoids Python recursion limits for long dependency chains.
    done = set()
    for start in touched & graph.keys():
        stack, active = [(start, False)], set()
        while stack:
            node, leaving = stack.pop()
            if leaving:
                active.discard(node)
                done.add(node)
            elif node in active:
                raise ValueError('Posible dependencia circular entre medidas; revisa las expresiones del bloque')
            elif node not in done:
                active.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in graph.get(node, ()))
    return plan, list(dict.fromkeys(warnings))


RECOVERY_VOLATILE = {'modifiedTime', 'structureModifiedTime', 'state', 'errorMessage'}


def dependency_report(client):
    """Read engine dependencies when supported, explicitly disclose missing scope."""
    report = {'engine': False, 'complete': False, 'rows': [], 'warnings': [
        'No se verifican todos los usos en visuales, filtros, marcadores ni otros objetos del informe; el MCP consulta el modelo semantico, no el informe completo.']}
    try:
        fields = ai_query_fields(client, {'query': 'EVALUATE INFO.CALCDEPENDENCY()', 'maxRows': 100000})
        rows = dax_rows(request_call(client, 'dax_query_operations', 'Execute', **fields))
        required = {'[OBJECT]', '[REFERENCED_OBJECT]', '[OBJECT_TYPE]', '[REFERENCED_OBJECT_TYPE]'}
        if rows and not required <= rows[0].keys():
            raise RuntimeError('El motor devuelve otro formato de dependencias')
        report.update(engine=True, complete=len(rows) < 100000, rows=rows)
        if not report['complete']:
            report['warnings'].append('La respuesta de dependencias alcanzo el limite; el resultado es parcial.')
    except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
        report['warnings'].append('No fue posible verificar dependencias con el motor: ' + str(exc) + '. Se usa el analisis DAX local, que es aproximado.')
        if not client.connection_name:
            raise RuntimeError('Conexion perdida durante la consulta de dependencias') from exc
    return report


def check_engine_dependencies(plan, report):
    if not report['engine']:
        return
    changes = {}
    deletes = {}
    for i, item in enumerate(plan):
        op = item['instruction']
        if item['tool'] not in ('measure_operations', 'column_operations'):
            continue
        key = (item['tool'], op['table'].casefold(), op['name'].casefold())
        changes.setdefault(key, []).append((i, item))
        if item['operation'] == 'Delete':
            deletes[key] = i
    for row in report['rows']:
        target_type = str(row.get('[REFERENCED_OBJECT_TYPE]', '')).upper()
        target_tool = 'measure_operations' if target_type == 'MEASURE' else 'column_operations'
        key = (target_tool, str(row.get('[REFERENCED_TABLE]', '')).casefold(), str(row.get('[REFERENCED_OBJECT]', '')).casefold())
        if key not in deletes:
            continue
        source_type = str(row.get('[OBJECT_TYPE]', '')).upper()
        source_tool = 'measure_operations' if source_type == 'MEASURE' else 'column_operations'
        source = (source_tool, str(row.get('[TABLE]', '')).casefold(), str(row.get('[OBJECT]', '')).casefold())
        if source == key:
            continue
        resolved = False
        for index, change in changes.get(source, []):
            if index >= deletes[key]:
                break
            if change['operation'] == 'Delete':
                resolved = True
            elif 'expression' in change['instruction']:
                refs = dax_references(change['instruction']['expression'])
                resolved = not any(name == key[2] and (table is None or table == key[1]) for table, name in refs)
        if not resolved:
            if plan[deletes[key]]['instruction'].get('force'):
                report['warnings'].append('FORZADA: dependencia del motor: ' + str(row.get('[TABLE]', '?')) +
                                          '[' + str(row.get('[OBJECT]', '?')) + ']. Puede eliminarse o quedar invalida.')
                continue
            raise ValueError('Dependencia detectada por el motor: ' + str(row.get('[TABLE]', '?')) +
                             '[' + str(row.get('[OBJECT]', '?')) + '] utiliza el objeto que se intenta eliminar. Modifica o elimina primero el dependiente.')


def target_state(client, tool, reference):
    matches = [row for row in list_objects(client, tool) if row.get('name') == reference['name'] and
               row.get('tableName') == reference['tableName']]
    return get_object(client, tool, reference) if matches else None


def stable_state(value):
    if isinstance(value, dict):
        value = dict(value)
        if 'name' in value and 'tableName' in value:
            for key in ('description', 'displayFolder', 'formatString'):
                value[key] = value.get(key) or ''
            for key in ('annotations', 'extendedProperties'):
                value[key] = value.get(key) or []
        return {key: stable_state(item) for key, item in value.items() if key not in RECOVERY_VOLATILE}
    if isinstance(value, list):
        return [stable_state(item) for item in value]
    return value


def check_semantic_state(state):
    if state is None:
        return
    error = state.get('errorMessage')
    status = str(state.get('state', '')).casefold()
    if (isinstance(error, str) and error.strip()) or 'error' in status:
        raise RuntimeError('El motor marco el objeto con error semantico: ' +
                           str(error or state.get('state')))


def save_recovery_file(path, data):
    """Persist before writing the model. Atomic replacement prevents half a journal."""
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def create_plan_backup(client, plan):
    folder = Path(getattr(client, 'backup_dir', Path.cwd() / 'backups'))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('bloque_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex + '.json')
    data = {'format': 1, 'model': client.model_name, 'connectionName': client.connection_name,
            'targetIdentity': selected_target(client), 'mcpSession': getattr(client, 'mcp_session_token', None),
            'connectionDiagnostics': list(getattr(client, 'connection_events', [])),
            'instance': {key: (client.instance or {}).get(key) for key in ('port', 'processId', 'startTime', 'parentWindowTitle')},
            'status': 'preparado', 'recovery': 'no_necesaria', 'targets': [], 'journal': [],
            'plan': [item['instruction'] for item in plan],
            'scope': 'Estado de objetos del modelo; no es una copia completa del PBIX/PBIP ni del informe.'}
    seen = set()
    if any(item['instruction'].get('force') for item in plan):
        data['forceSnapshot'] = metadata_export(client)
        if not data['forceSnapshot']['complete']:
            raise RuntimeError('No se puede respaldar todo el alcance de la cascada: ' + '; '.join(data['forceSnapshot']['errors']))
        # TMDL also retains objects outside the basic tables/columns/measures scope.
        data['forceTmdl'] = request_call(client, 'model_operations', 'ExportTMDL',
                                       tmdlExportOptions={'serializationOptions': {'includeChildren': True},
                                                          'maxReturnCharacters': -1})
    for item in plan:
        if item['operation'] == 'Execute':
            continue
        ref = object_reference(ai_object(item['instruction']))
        key = (item['tool'], ref['tableName'], ref['name'])
        if key in seen:
            continue
        seen.add(key)
        before = target_state(client, item['tool'], ref)
        target = {'tool': item['tool'], 'reference': ref, 'before': before}
        if before and operation_supported(client, item['tool'], 'ExportTMDL'):
            try:
                target['tmdlExport'] = request_call(client, item['tool'], 'ExportTMDL', references=[ref],
                                                  tmdlExportOptions={'maxReturnCharacters': -1})
            except (RuntimeError, ValueError) as exc:
                target['tmdlExportError'] = str(exc)
                print('AVISO: respaldo JSON disponible; no se pudo exportar TMDL:', exc)
        data['targets'].append(target)
    save_recovery_file(path, data)
    print('Respaldo local:', path.resolve())
    client.last_backup_path = str(path.resolve())
    return path, data


def transaction_request(client, operation, transaction_id=None):
    req = {'operation': operation}
    if operation == 'Begin':
        req['connectionName'] = client.connection_name
    else:
        req['transactionId'] = transaction_id
    root = tool_schema(client, 'transaction_operations')
    validate_schema(root, root, {'request': req})
    return unwrap_result(client.tool('transaction_operations', {'request': req}))


def transaction_id(value):
    if isinstance(value, dict):
        if isinstance(value.get('transactionId'), str):
            return value['transactionId']
        for child in value.values():
            found = transaction_id(child)
            if found:
                return found
    return None


def restore_definition(client, tool, state):
    props = definition_properties(client, tool)
    result = {key: value for key, value in state.items() if key in props and key not in RECOVERY_VOLATILE}
    if tool == 'measure_operations':
        result.pop('dataType', None)  # Inferred by the expression, not freely writable.
    for key in ('description', 'displayFolder', 'formatString'):
        if key in props:
            result[key] = state.get(key) or ''
    for key in ('annotations', 'extendedProperties'):
        if key in props:
            result[key] = state.get(key) or []
    return result


def verify_backup(client, backup):
    differences = []
    for target in backup['targets']:
        try:
            current = target_state(client, target['tool'], target['reference'])
            if stable_state(current) != stable_state(target['before']):
                differences.append(target['reference']['tableName'] + '[' + target['reference']['name'] + ']: estado distinto')
            elif current and target['before']:
                before_error = target['before'].get('errorMessage') or 'error' in str(target['before'].get('state', '')).casefold()
                if not before_error:
                    check_semantic_state(current)
                    if target['tool'] == 'column_operations' and calculated_column(target['before']):
                        verify_column_storage(client, target['reference'])
        except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
            differences.append(target['reference']['name'] + ': no se pudo verificar: ' + str(exc))
    if backup.get('forceSnapshot'):
        current = metadata_export(client)
        differences.extend(snapshot_differences(backup['forceSnapshot'], current))
        try:
            tmdl = request_call(client, 'model_operations', 'ExportTMDL',
                               tmdlExportOptions={'serializationOptions': {'includeChildren': True},
                                                  'maxReturnCharacters': -1})
            if tmdl != backup.get('forceTmdl'):
                differences.append('El TMDL del modelo difiere del respaldo anterior a la cascada')
        except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
            differences.append('No se pudo verificar el TMDL despues de la cascada: ' + str(exc))
    return differences


def snapshot_differences(before, after):
    differences = list(after.get('errors', []))
    for section in ('tables', 'measures', 'columns', 'relationships'):
        def indexed(snapshot):
            return {(r.get('tableName', ''), r['name']):
                    {'metadata': stable_state(r), 'errorMessage': r.get('errorMessage') or '',
                     'semanticError': 'error' in str(r.get('state', '')).casefold()}
                    for r in snapshot.get(section, [])}
        previous, current = indexed(before), indexed(after)
        for key in previous.keys() | current.keys():
            if previous.get(key) != current.get(key):
                differences.append(f'{section}: {key[0]}[{key[1]}]: metadatos distintos')
    return differences


def recover_plan(client, path, backup, tx):
    print('\nRECUPERACION: intentando restaurar el estado anterior...')
    recovery_errors = []
    if not backup['journal']:
        if tx:
            try:
                transaction_request(client, 'Rollback', tx)
                backup['transactionClosed'] = True
                client.active_transaction = None
            except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
                recovery_errors.append('No se pudo confirmar el cierre de la transaccion sin escrituras: ' + str(exc))
        backup['recovery'] = 'no_necesaria'
        backup['recoveryErrors'] = recovery_errors
        save_recovery_file(path, backup)
        print('No se envio ninguna escritura del bloque; no hay cambios del bloque que deshacer.')
        for error in recovery_errors:
            print('AVISO:', error)
        return not recovery_errors
    expected_identity = backup.get('targetIdentity')
    if expected_identity and not same_target(expected_identity, selected_target(client)):
        backup['recovery'] = 'incompleta'
        backup['recoveryErrors'] = ['El destino seleccionado cambio; no se modificara otro modelo para recuperar este bloque']
        save_recovery_file(path, backup)
        print('RECUPERACION DETENIDA: la identidad del modelo cambio.')
        return False
    if tx:
        process = getattr(client, 'process', None)
        if process is not None and process.poll() is not None:
            ensure_connected(client, allow_reconnect=True, allow_restart=True)
        old_token = backup.get('mcpSession')
        if old_token and old_token != getattr(client, 'mcp_session_token', None):
            if old_token in getattr(client, 'retired_mcp_sessions', []):
                backup['transactionClosed'] = True
                backup['sessionRecovery'] = 'Proceso MCP original terminado; estado inspeccionado desde una nueva sesion'
            else:
                recovery_errors.append('No se puede reutilizar transactionId en otra sesion MCP')
        if not backup.get('transactionClosed'):
            try:
                transaction_request(client, 'Rollback', tx)
                backup['transactionClosed'] = True
                client.active_transaction = None
            except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
                recovery_errors.append('Rollback del servidor: ' + str(exc))
        if backup.get('transactionClosed'):
            client.active_transaction = None
            ensure_connected(client, allow_reconnect=True, allow_restart=True)
        differences = verify_backup(client, backup) if client.connection_name else ['Conexion perdida; no se pudo verificar el modelo']
        if not differences and backup.get('transactionClosed'):
            backup['recovery'] = 'verificada'
            backup['recoveryErrors'] = recovery_errors
            save_recovery_file(path, backup)
            print('RECUPERACION VERIFICADA: los objetos afectados coinciden con el respaldo previo.')
            return True
        # Never issue compensating writes while a server transaction may still be open.
        if not backup.get('transactionClosed'):
            backup['recovery'] = 'incompleta'
            backup['recoveryErrors'] = recovery_errors + differences
            save_recovery_file(path, backup)
            print('RECUPERACION NO VERIFICADA. Conserva el respaldo:', path)
            return False
    if ensure_connected(client, allow_reconnect=True, allow_restart=True) and (
            same_target(expected_identity, selected_target(client)) if expected_identity else client.connection_name == backup['connectionName']):
        for entry in reversed(backup['journal']):
            if entry.get('operation') == 'Execute':
                continue
            tool, ref, before = entry['tool'], entry['reference'], entry['before']
            try:
                current = target_state(client, tool, ref)
                if stable_state(current) == stable_state(before):
                    continue
                if before is None:
                    fields = build_object_fields(client, tool, 'Delete', ref)
                    request_call(client, tool, 'Delete', **fields)
                else:
                    operation = 'Update' if current else 'Create'
                    definition = restore_definition(client, tool, before)
                    fields = build_object_fields(client, tool, operation, definition)
                    request_call(client, tool, operation, **fields)
            except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
                recovery_errors.append(ref['name'] + ': ' + str(exc))
        restored_columns = [{'tool': target['tool'], 'operation': 'Update',
                             'instruction': {'table': target['reference']['tableName'], 'name': target['reference']['name']}}
                            for target in backup['targets'] if target['tool'] == 'column_operations' and target['before'] is not None]
        if restored_columns:
            try:
                calculate_plan_columns(client, restored_columns)
            except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
                recovery_errors.append('Recalculo despues de restaurar columnas: ' + str(exc))
    else:
        recovery_errors.append('No se puede restaurar con certeza la conexion original')
    differences = verify_backup(client, backup) if client.connection_name else ['Conexion perdida; verificacion pendiente']
    backup['recovery'] = 'verificada' if not differences else 'incompleta'
    backup['recoveryErrors'] = recovery_errors + differences
    save_recovery_file(path, backup)
    if not differences:
        print('RECUPERACION VERIFICADA para los objetos respaldados; no implica restauracion del informe completo.')
        return True
    print('RECUPERACION INCOMPLETA. Diferencias o errores:')
    for message in recovery_errors + differences:
        print('-', message)
    print('Respaldo para revision:', path)
    return False


def execute_ai_plan(client, plan):
    """--write authorizes this validated block; never retry an uncertain write."""
    if any(item['operation'] != 'Execute' for item in plan) and not client.writable:
        raise RuntimeError('Modo solo lectura: el bloque contiene escrituras')
    forced = [item for item in plan if item['instruction'].get('force')]
    if forced:
        if not all(item.get('forceAuthorized') is True for item in forced):
            raise RuntimeError('Eliminacion forzada sin CONFIRMAR FORZAR; no se enviaron cambios')
        if any(item['operation'] == 'Execute' for item in plan):
            raise ValueError('Separa las consultas execute_dax del bloque forzado para conservar la transaccion global')
        if 'transaction_operations' not in client.tools or not all(operation_supported(client, 'transaction_operations', op) for op in ('Begin', 'Commit', 'Rollback')):
            raise RuntimeError('El modo forzado requiere transacciones del servidor')
        if 'model_operations' not in client.tools or not operation_supported(client, 'model_operations', 'ExportTMDL'):
            raise RuntimeError('El modo forzado requiere respaldo TMDL del modelo')
    if getattr(client, 'active_transaction', None):
        raise RuntimeError('Hay una transaccion anterior pendiente de cierre; no se iniciara otro bloque')
    if not ensure_connected(client, allow_reconnect=True, allow_restart=True):
        raise RuntimeError('No se puede iniciar el bloque: ' + str(getattr(client, 'connection_issue', None) or 'conexion no disponible'))
    previous_scope = getattr(client, '_auto_write_scope', False)
    previous_block = getattr(client, '_block_active', False)
    client._auto_write_scope = bool(client.writable)
    client._block_active = True
    try:
        return _execute_ai_plan(client, plan)
    finally:
        client._auto_write_scope = previous_scope
        client._block_active = previous_block


class BlockOperationError(RuntimeError):
    def __init__(self, index, reference, message):
        self.operation_index = index
        super().__init__(f'Operacion {index}, {reference["tableName"]}[{reference["name"]}]: {message}')


def dax_column_reference(table, column):
    return "'" + table.replace("'", "''") + "'[" + column.replace(']', ']]') + ']'


def verify_column_storage(client, reference):
    query = 'EVALUATE ROW("__MCP_ColumnReady", COUNTROWS(VALUES(' + dax_column_reference(reference['tableName'], reference['name']) + ')))'
    fields = ai_query_fields(client, {'query': query, 'maxRows': 1})
    dax_rows(request_call(client, 'dax_query_operations', 'Execute', **fields))


def calculate_plan_columns(client, plan):
    """Calculate storage, then query the engine; metadata alone is insufficient."""
    columns = {}
    for index, item in enumerate(plan, 1):
        if item['tool'] != 'column_operations':
            continue
        op = item['instruction']
        key = (op['table'], op['name'])
        if item['operation'] == 'Delete':
            columns.pop(key, None)
        else:
            columns[key] = index
    tables = list(dict.fromkeys(table for table, _ in columns))
    for table in tables:
        ref = next({'tableName': t, 'name': name} for t, name in columns if t == table)
        index = columns[table, ref['name']]
        try:
            if not operation_supported(client, 'table_operations', 'RefreshWithXMLA'):
                raise RuntimeError('El servidor no ofrece RefreshWithXMLA para calcular las columnas')
            print('Calculando columnas de:', table)
            request_call(client, 'table_operations', 'RefreshWithXMLA', references=[{'name': table}], refreshType='Calculate')
        except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
            # A table-level Calculate error may actually belong to another new
            # column in that table. Inspect all affected columns before naming it.
            for (target_table, name), affected_index in columns.items():
                if target_table != table or not client.connection_name:
                    continue
                candidate_ref = {'tableName': table, 'name': name}
                try:
                    state = get_object(client, 'column_operations', candidate_ref)
                except (RuntimeError, ValueError, TimeoutError, OSError):
                    continue
                try:
                    check_semantic_state(state)
                except RuntimeError as semantic:
                    raise BlockOperationError(affected_index, candidate_ref, str(semantic)) from exc
            raise BlockOperationError(index, ref, 'No se pudo calcular la tabla: ' + str(exc)) from exc
        for (target_table, name), index in columns.items():
            if target_table != table:
                continue
            ref = {'tableName': table, 'name': name}
            try:
                verify_column_storage(client, ref)
                check_semantic_state(get_object(client, 'column_operations', ref))
            except (RuntimeError, ValueError, TimeoutError, OSError) as exc:
                raise BlockOperationError(index, ref, 'La columna no puede evaluarse despues del recalculo: ' + str(exc)) from exc
    return tables


def target_operation_index(plan, target):
    return max(i for i, item in enumerate(plan, 1) if item['tool'] == target['tool'] and
               item['instruction'].get('table') == target['reference']['tableName'] and
               item['instruction'].get('name') == target['reference']['name'])


def _execute_ai_plan(client, plan):
    """Back up first; use server transaction when available, otherwise compensate."""
    identity = selected_target(client)
    mcp_session = getattr(client, 'mcp_session_token', None)
    writes = any(item['operation'] != 'Execute' for item in plan)
    path, backup = create_plan_backup(client, plan) if writes else (None, None)
    tx = None
    client.last_recovery = 'no_necesaria'
    has_queries = any(item['operation'] == 'Execute' for item in plan)
    if writes and not has_queries and 'transaction_operations' in client.tools and all(
            operation_supported(client, 'transaction_operations', op) for op in ('Begin', 'Commit', 'Rollback')):
        tx = transaction_id(transaction_request(client, 'Begin'))
        if not tx:
            backup['status'] = 'inicio_transaccion_no_confirmado'
            save_recovery_file(path, backup)
            raise RuntimeError('El servidor no devolvio transactionId. No se enviaron cambios del bloque.')
        backup['transactionId'] = tx
        client.active_transaction = tx
        try:
            save_recovery_file(path, backup)
        except OSError:
            transaction_request(client, 'Rollback', tx)
            raise
        print('Proteccion: transaccion del servidor; la recuperacion se verificara por lectura.')
    elif writes:
        print('Proteccion: respaldo y compensacion de cambios; este bloque no usa transaccion global.')
        if has_queries:
            print('El bloque contiene consultas DAX: se usa compensacion para que vean los cambios ya aplicados; el servidor DAX utiliza otra sesion.')
    results = []
    failed = False
    pending_columns = False
    for index, item in enumerate(plan, 1):
        op, tool, operation = item['instruction'], item['tool'], item['operation']
        try:
            if not ensure_connected(client, allow_reconnect=not bool(tx), allow_restart=False):
                raise RuntimeError('No se pudo verificar la conexion antes de enviar la operacion: ' + str(getattr(client, 'connection_issue', None)))
            if not same_target(identity, selected_target(client)):
                raise ConnectionIdentityError('La identidad del modelo cambio; operacion no enviada')
            if tx and getattr(client, 'mcp_session_token', None) != mcp_session:
                raise ConnectionIdentityError('La sesion MCP cambio durante la transaccion; operacion no enviada')
            fields = dict(item['fields'])
            if operation == 'Execute' and pending_columns:
                backup['calculatedTables'] = calculate_plan_columns(client, plan[:index-1])
                pending_columns = False
            if operation != 'Execute':
                ref = object_reference(ai_object(op))
                backup['journal'].append({'index': index, 'tool': tool, 'operation': operation, 'reference': ref,
                                          'before': target_state(client, tool, ref), 'status': 'enviando'})
                backup['status'] = 'ejecutando'
                save_recovery_file(path, backup)
                if tx and 'options' in fields:
                    fields['options'] = dict(fields['options'], useTransaction=False)
            if tool == 'column_operations':
                pending_columns = True
            value = request_call(client, tool, operation, **fields)
            if tool == 'dax_query_operations':
                rows = dax_rows(value)
                display_grid(rows)
                if len(rows) >= item['fields'].get('maxRows', 1000):
                    print('Se alcanzo el limite de filas; el resultado puede estar truncado.')
            elif not verify_write(client, tool, operation, ai_object(op)):
                raise RuntimeError('Resultado no confirmado por lectura; revisa el modelo antes de reintentar')
            if operation != 'Execute' and not tx and operation != 'Delete':
                check_semantic_state(target_state(client, tool, object_reference(ai_object(op))))
            if operation != 'Execute':
                backup['journal'][-1]['status'] = 'verificada'
                save_recovery_file(path, backup)
            label = 'Verificada, pendiente de Commit' if tx else 'Correcta'
            print(f'[{index}/{len(plan)}] {label}: {AI_LABELS[op["action"]]} ' + op.get('name', 'consulta'))
            results.append({'index': index, 'action': op['action'], 'status': 'correcta'})
        except (ValueError, RuntimeError, TimeoutError, OSError, KeyError, TypeError, KeyboardInterrupt) as exc:
            invalidate_on_error(client, exc)
            print(f'[{index}/{len(plan)}] ERROR: {exc}')
            results.append({'index': index, 'action': op['action'], 'status': 'fallida', 'error': str(exc)})
            failed = True
            break
    if not failed and tx:
        try:
            final = [(target, target_state(client, target['tool'], target['reference'])) for target in backup['targets']]
            transaction_request(client, 'Commit', tx)
            backup['transactionClosed'] = True
            client.active_transaction = None
            if pending_columns:
                # Do not let a table-level refresh obscure an invalid measure or
                # column that the engine can already identify after Commit.
                for target, _ in final:
                    try:
                        check_semantic_state(target_state(client, target['tool'], target['reference']))
                    except RuntimeError as exc:
                        raise BlockOperationError(target_operation_index(plan, target), target['reference'], str(exc)) from exc
                backup['calculatedTables'] = calculate_plan_columns(client, plan)
                pending_columns = False
            for target, expected in final:
                current = target_state(client, target['tool'], target['reference'])
                try:
                    check_semantic_state(current)
                except RuntimeError as exc:
                    raise BlockOperationError(target_operation_index(plan, target), target['reference'], str(exc)) from exc
                # SaveChanges/Commit resolves inferred types. Compare requested types
                # exactly, but do not mistake normal inference for a failed commit.
                explicit_type = any(item['tool'] == target['tool'] and
                                    item['instruction'].get('name') == target['reference']['name'] and
                                    item['instruction'].get('table') == target['reference']['tableName'] and
                                    'dataType' in item['instruction'] for item in plan)
                if current is not None and expected is not None and not explicit_type:
                    current, expected = dict(current), dict(expected)
                    current.pop('dataType', None)
                    expected.pop('dataType', None)
                if stable_state(current) != stable_state(expected):
                    raise BlockOperationError(target_operation_index(plan, target), target['reference'], 'El estado despues de Commit no coincide con el esperado')
        except (ValueError, RuntimeError, TimeoutError, OSError, KeyboardInterrupt) as exc:
            failed = True
            backup['commitError'] = str(exc)
            if isinstance(exc, BlockOperationError):
                backup['failedOperationIndex'] = exc.operation_index
                for row in results:
                    if row['index'] == exc.operation_index:
                        row.update(status='fallida', error=str(exc))
            print('ERROR AL CONFIRMAR LA TRANSACCION:', exc)
    if not failed and not tx and pending_columns:
        try:
            backup['calculatedTables'] = calculate_plan_columns(client, plan)
        except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
            failed = True
            backup['calculationError'] = str(exc)
            if isinstance(exc, BlockOperationError):
                backup['failedOperationIndex'] = exc.operation_index
                for row in results:
                    if row['index'] == exc.operation_index:
                        row.update(status='fallida', error=str(exc))
            print('ERROR AL CALCULAR COLUMNAS:', exc)
    if writes:
        backup['results'] = results
        backup['connectionDiagnostics'] = list(getattr(client, 'connection_events', []))
        backup['status'] = 'fallido' if failed else 'completado'
        if failed:
            recover_plan(client, path, backup, tx)
            client.last_recovery = backup['recovery']
            for row in results:
                row['recovery'] = backup['recovery']
            backup['connectionDiagnostics'] = list(getattr(client, 'connection_events', []))
            save_recovery_file(path, backup)
        else:
            if backup.get('forceSnapshot'):
                after = metadata_export(client)
                backup['forceEffects'] = snapshot_differences(backup['forceSnapshot'], after)
                print('Efectos del bloque forzado (incluye cambios solicitados y efectos de cascada):')
                for effect in backup['forceEffects']:
                    print('-', effect)
                if after['errors']:
                    print('AVISO: inspeccion final parcial; consulta el respaldo.')
                for section in ('measures', 'columns'):
                    for obj in after.get(section, []):
                        if obj.get('errorMessage') or 'error' in str(obj.get('state', '')).casefold():
                            print('AVISO: objeto invalido tras forzar:', obj.get('tableName'), obj.get('name'), obj.get('errorMessage', obj.get('state')))
            save_recovery_file(path, backup)
    client.last_block_status = 'fallido' if failed else 'completado'
    skipped = len(plan) - len(results)
    print('\nRESULTADO FINAL')
    print('Verificadas antes de recuperacion:' if writes and failed else 'Correctas:',
          sum(r['status'] == 'correcta' for r in results))
    print('Fallidas:', sum(r['status'] == 'fallida' for r in results) +
          int(bool(backup and backup.get('commitError') and not backup.get('failedOperationIndex'))))
    print('No ejecutadas:', skipped)
    print('Estado final del bloque:', client.last_block_status)
    if writes and failed:
        print('Estado de recuperacion:', client.last_recovery, '(los exitos anteriores no equivalen a cambios conservados)')
        if client.last_recovery == 'verificada':
            print('Cambios del bloque conservados: 0; se restauro el estado anterior de los objetos respaldados.')
        if backup.get('commitError'):
            print('Fallo final de Commit:', backup['commitError'])
    for row in results:
        if row.get('error'):
            print(f'Detalle del error [{row["index"]}]: {row["error"]}')
    return results


def ai_block(client):
    if not ensure_connected(client):
        return []
    print('Pega el bloque JSON generado por IA. Escribe FIN en una linea independiente para terminar:')
    lines = []
    while True:
        line = input()
        if line.strip() == 'FIN':
            break
        lines.append(line)
    operations = parse_ai_block('\n'.join(lines))
    print('Validando instrucciones...')
    plan, warnings = validate_ai_block(client, operations)
    dependencies = dependency_report(client)
    check_engine_dependencies(plan, dependencies)
    warnings.extend(dependencies['warnings'])
    print('\nModelo de destino:', client.model_name)
    counts = {}
    for item in plan:
        action = item['instruction']['action']
        counts[action] = counts.get(action, 0) + 1
    print('Operaciones detectadas:')
    for action, count in counts.items():
        print(f'- {AI_LABELS[action]}: {count}')
    print('Total:', len(plan), 'operaciones')
    print('Vista previa completa en orden:')
    for i, item in enumerate(plan, 1):
        op = item['instruction']
        print(f'{i}. {AI_LABELS[op["action"]]} ' + (f'{op["table"]}[{op["name"]}]' if 'name' in op else 'consulta DAX'))
    for warning in warnings:
        print('AVISO:', warning)
    print('Dependencias del motor:', len(dependencies['rows']) if dependencies['engine'] else 'no disponibles')
    if getattr(client, 'debug', False):
        pretty([item['instruction'] for item in plan])
    if any(item['operation'] != 'Execute' for item in plan):
        print('Se creara un respaldo local antes de escribir. Si falla el bloque se intentara restaurar y verificar el estado anterior. No es una copia completa del PBIX/PBIP.')
    if any(item['operation'] == 'Delete' for item in plan):
        print('ATENCION: se eliminaran los objetos indicados. Cascada solo en operaciones con force: true.')
    forced = [item for item in plan if item['instruction'].get('force')]
    if forced:
        print('FORZAR puede eliminar dependientes o dejar expresiones/visuales invalidos. No garantiza recuperar el informe completo.')
        if input('Escribe CONFIRMAR FORZAR para autorizar la cascada de este bloque: ').strip() != 'CONFIRMAR FORZAR':
            print('Bloque cancelado; no se envio ninguna escritura.')
            return []
        for item in forced:
            item['forceAuthorized'] = True
    print('Ejecutando bloque validado' + ('; escritura autorizada por --write.' if client.writable else '; solo consultas de lectura.'))
    return execute_ai_plan(client, plan)


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
    parser.add_argument('--backup-dir', type=Path, default=Path.cwd() / 'backups', help='Carpeta local de respaldos de bloques')
    opts = parser.parse_args()
    if opts.write:
        print('MODO EDICION. Haz copia del PBIX antes de modificarlo.')
        print('--write autoriza las escrituras de los bloques de la opcion 13, sin confirmaciones adicionales.')
    else:
        print('SOLO LECTURA. Para editar: python cliente.py --write')
    client = MCP(opts.write, opts.server, opts.debug)
    client.backup_dir = opts.backup_dir
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
            print('13. Ejecutar bloque de instrucciones de IA')
            print('14. Exportar JSON completo: tablas, medidas y columnas calculadas')
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
                elif choice == '13':
                    ai_block(client)
                elif choice == '14':
                    if ensure_connected(client):
                        print_metadata_export(client)
                else:
                    print('Opcion invalida')
            except (ValueError, RuntimeError, TimeoutError, KeyError, TypeError, OSError) as exc:
                print('ERROR:', exc)
                invalidate_on_error(client, exc)
            if choice not in ('0', '13'):
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

