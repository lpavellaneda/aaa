# Cliente Python para Power BI Desktop

Cliente de consola refactorizado a partir de `cliente_powerbi.py`, con Python 3.12
y exclusivamente su biblioteca estándar. No utiliza Node.js, npm, pip ni servicios
de IA para funcionar. Necesita el servidor MCP de Microsoft instalado por separado.

## Validación DAX con datos, relaciones y columnas calculadas

La versión actual conserva el modo automático de `--write` y añade correcciones
en el análisis de dependencias, la identificación de errores posteriores a Commit
y el procesamiento de columnas calculadas. Todo el desarrollo y las pruebas se
realizaron en la PC de trabajo con el ejecutable de la extensión de VS Code.

### Correcciones verificadas

- `VAR ... RETURN [Medida]` ya no se interpreta como una tabla llamada RETURN.
  El escáner distingue palabras de control de tablas entre comillas. Esto permite
  detectar referencias posteriores, ciclos y eliminaciones de dependencias nuevas
  del mismo bloque. No se eliminó la comprobación de dependencias.
- Una columna calculada no se considera utilizable solo porque Get devuelva su
  expresión. Se agrupan las tablas con columnas afectadas y se solicita
  `table_operations.RefreshWithXMLA` con `refreshType: Calculate`. No se solicita
  una recarga Full de los datos importados.
- En bloques solo de escrituras, Calculate se realiza después de Commit. En
  bloques mixtos, se hace antes de la consulta DAX que necesita las columnas y al
  terminar si siguen pendientes. Se ejecuta una consulta al motor que obliga a
  acceder a su almacenamiento; un estado de metadatos por sí solo no prueba que
  la columna contenga datos procesados.
- La recuperación por compensación recalcula las columnas que restauró y verifica
  que puedan consultarse. Si la expresión se restauró pero los datos siguen sin
  poder evaluarse, la recuperación se informa como incompleta.
- Los errores semánticos detectados después de Commit identifican el objeto y la
  última operación que lo afectó. Esa operación se marca fallida y no se cuenta el
  mismo error dos veces. Un error global de Commit sin objeto identificado se
  presenta como error global, sin inventar una operación causal.

Calculate puede recalcular otras columnas calculadas de la tabla afectada, además
de las creadas por el bloque. El respaldo sigue siendo de objetos y no de todo el
proyecto o de todas las filas. No se promete reversión integral de informes, datos
de origen o metadatos complejos no cubiertos por las pruebas.

### Suite real reutilizable

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python tests/live_complex_model.py --modelo "Titulo del modelo" --confirmar-escrituras
```

El argumento adicional pertenece al script de pruebas destructivas, no al uso
normal de la opción 13. En el cliente el flujo sigue siendo `--write`, elegir el
modelo, opción 13, JSON y FIN, sin confirmaciones adicionales.

El script inspecciona tablas, columnas, tipos, medidas y relaciones. Escoge una
relación activa muchos-a-uno con datos y columnas numéricas, otra tabla numérica y
una dimensión apropiada. No contiene nombres fijos de tablas o columnas de un
modelo particular. Si el modelo no reúne esos requisitos, se detiene con el
motivo; no inventa nombres ni presenta pruebas omitidas como exitosas.

Se probaron SUM, AVERAGE, COUNTROWS, DISTINCTCOUNT, CALCULATE, FILTER, ALL,
REMOVEFILTERS, VAR/RETURN, DIVIDE, SUMX y RELATED. Incluye referencias entre medidas
alojadas en tablas distintas, modificación de una base con dependientes y columnas
con medidas dependientes. Los importes se eligen con moneda compatible cuando los
nombres del modelo la identifican; los chequeos estructurales no se presentan
como indicadores financieros validados para decisiones de negocio.

Los valores esperados se contrastan con totales del modelo y agrupaciones cuyo
resultado se agrega también en Python. Se comprueba la cobertura del conteo de
filas y se rechazan verificaciones truncadas. Las cantidades de tipo Double se
comparan con tolerancia relativa 1e-12 y absoluta 1e-6. Después de cada bloque se
abre un lector MCP nuevo para verificar persistencia tras Commit, expresiones y
estados; después de errores también se comparan todos los objetos respaldados.
Los resultados numéricos comprueban filtros de dimensión, ALL/REMOVEFILTERS,
RELATED, medidas dependientes y datos de columnas procesadas.

### Resultado de la validación principal

- **135 pruebas automatizadas aprobadas:** las 119 anteriores y 16 regresiones nuevas.
- **31 casos reales aprobados:** bloques de 10, 20 y 50 operaciones, más los casos
  de dependencias, cálculo, errores, recuperación y limpieza.
- **101 comparaciones numéricas** y **20 objetos recuperados** comprobados desde
  lectores MCP nuevos, adicionales al conteo de casos.
- **10 casos negativos esperados:** cuatro rechazos previos sin escrituras y seis
  bloques con error intencional y recuperación verificada.
- Cero fallos inesperados en la ejecución final. Durante el desarrollo se
  reprodujeron y corrigieron la dependencia omitida tras RETURN y las columnas
  existentes pero aún sin datos procesados. Los reensayos no se cuentan como
  pruebas distintas en el total principal de **166 casos**.

El informe local `resultado_modelo_complejo.json` contiene las tablas y columnas
efectivamente elegidas, fórmulas, resultados esperados/observados, errores,
operaciones y limpieza. Se excluye de Git para no publicar el contenido del modelo.
El paquete de entrega incluye ese informe para el usuario. El código y la suite
genérica sí se publican en el repositorio.

La limpieza eliminó solo los objetos con el identificador único del ensayo y
comprobó el inventario original de medidas, columnas y relaciones. No se guardó
ni reabrió el PBIP/PBIX. No se ejecutaron pruebas en la otra laptop. Esa laptop
solo necesita reemplazar cliente.py; se conservan su extensión de VS Code, sus
variables de entorno y sus respaldos. codigo.txt debe actualizarse si se utiliza
el formato de transferencia. README.md y tests/ son opcionales para ejecución.


## --write sin confirmaciones y reconexión segura

Para actualizar otra laptop basta con reemplazar **cliente.py** por la versión
actual de la rama main. Es un único archivo y solo utiliza la biblioteca estándar.
Conserva la detección original del ejecutable en `Path.home() / '.vscode' / 'extensions'`;
no contiene la ruta personal, el puerto ni los nombres del modelo de este equipo.

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python cliente.py --write
```

Flujo: seleccionar el modelo, elegir 13, pegar JSON y terminar con FIN. El bloque
se valida completo, imprime un resumen y se ejecuta automáticamente. No solicita
EDITAR al inicio, CONFIRMAR, s/N, selección de detalle ni Enter para volver al menú
después de la opción 13. `--debug` muestra el detalle completo sin preguntar.
Sin --write, las escrituras siguen bloqueadas y los bloques de consultas DAX
pueden ejecutarse en modo lectura. Las validaciones de objetos, dependencias,
esquemas, errores semánticos, respaldo, transacciones y recuperación se conservan.
Los formularios de las opciones 4 a 9 conservan sus flujos anteriores.

El consentimiento booleano de escritura del servidor puede ser autorizado por
--write durante el bloque validado. Una solicitud desconocida, de credenciales o
de licencia se rechaza sin abrir preguntas: --write no acepta licencias nuevas
ni proporciona información adicional. El servidor puede rechazar una operación
por permisos o políticas y ese rechazo sigue siendo un error.

### Identidad y reconexión

La comparación anterior exigía igualdad del título de la ventana y de valores
sin normalizar de proceso/hora. Un cambio de título o representaciones distintas
del mismo PID/fecha podían rechazar una conexión válida. Además, se borraba el
destino seleccionado al perder la conexión y se comparaba el alias MCP como si
fuera la identidad del modelo. Son defectos identificados y reproducidos en las
pruebas; sin el registro de la otra laptop no se puede afirmar cuál fue su causa
exacta.

Ahora se separan:

- **Destino:** puerto local, proceso y hora de inicio normalizada, más la identidad
  de base de datos devuelta por GetConnection. El título es informativo y no se
  usa para cambiar de modelo automáticamente.
- **Conexión:** alias devuelto por Connect, que puede cambiar al reconectar.
- **Sesión:** identidad de la sesión del motor y del proceso MCP; un transactionId
  no se reutiliza ciegamente en otro proceso.

Ante una conexión perdida se conserva la intención de destino. Se descubre la
misma instancia y se comprueba la misma base de datos antes de volver a escribir.
No se cambia de puerto, no se busca otra instancia por el mismo título y se rechaza
una base distinta aunque coincidan alias o nombre de ventana. Si no hay datos
suficientes para probar la identidad, el bloque se detiene con un diagnóstico.

Si el proceso MCP murió, el cliente puede iniciar otro con el mismo ejecutable y
modo, redescubrir herramientas y reconectar al destino verificado. Dentro de una
transacción no se continúa escribiendo sobre una nueva sesión: se detiene el
bloque y se intenta recuperar, comprobando el estado desde la sesión nueva cuando
se confirmó que el proceso anterior terminó.

Una escritura enviada con respuesta incierta **nunca se repite automáticamente**.
Se consulta el estado y se aplica el procedimiento de recuperación. Los respaldos
incluyen `targetIdentity`, sesión MCP y `connectionDiagnostics` con motivos de
rechazo/reconexión. Si el fallo sucede antes de enviar escrituras, se informa que
no hay cambios del bloque que deshacer, distinguiéndolo de una recuperación fallida.
Una transacción anterior sin cierre confirmado impide iniciar otro bloque.

### Pruebas de esta corrección

La corrección de conexión pasó **119 pruebas automatizadas**, incluidas en las 135 actuales. Entre ellas: flujo real de main sin
EDITAR, ninguna pregunta después de FIN, escritura deshabilitada sin --write,
título cambiado, PID/puerto como texto, fechas UTC equivalentes, fecha opcional
ausente, proceso/puerto/base distintos, pérdida de alias, sesión cambiada durante
transacción, respuesta perdida después de escribir, autorización booleana del
servidor y rechazo de solicitudes desconocidas.

Pruebas reales con el ejecutable de VS Code y el modelo abierto:

| Caso | Resultado |
| --- | --- |
| Bloque de 20 operaciones | Creaciones, modificaciones y eliminaciones; solo JSON y FIN |
| Título y formatos de descubrimiento modificados por el ensayo | Misma instancia real aceptada, sin confundir formato con cambio de modelo |
| Disconnect real de la conexión MCP | Reconexión al mismo destino y bloque completado |
| Proceso MCP terminado antes de un bloque | Nuevo proceso, mismo modelo verificado y bloque completado |
| Respuesta perdida tras una creación aplicada | Una sola creación enviada; recuperación verificada |
| Proceso terminado tras escritura sin transacción global | Reinicio, lectura del estado y compensación verificada |
| Proceso terminado dentro de una transacción | Recuperación en una sesión nueva; sin reutilizar la transacción anterior |
| DAX intencionalmente inválido | Bloque fallido y recuperación verificada |
| Otra instancia mediante datos inyectados | Rechazada; ninguna escritura a otro modelo |
| Limpieza desde un lector MCP nuevo | Ausencia de los objetos de prueba verificada |

La pérdida de respuesta se inyecta **después** de enviar y aplicar la escritura
real; no se simula la escritura. La terminación del proceso MCP y Disconnect son
reales. Los cambios de formato/título y el destino alternativo son datos inyectados,
indicados como tales; no se afirma haber cambiado físicamente a otro modelo.
Se renovó la sesión del lector independiente antes de comprobar la limpieza,
porque el servidor puede mantener metadatos en caché de una conexión anterior.

```powershell
python -m unittest discover -s tests -v
# Solo en un modelo autorizado para pruebas destructivas:
python tests/live_auto_connection.py --modelo "Titulo del modelo" --confirmar-escrituras
```

El informe real se guarda en `resultado_automatico_conexion.json`. Para investigar
un rechazo en otro equipo, utiliza --debug y conserva el error específico y el
respaldo con connectionDiagnostics. No se garantiza recuperación si la instancia
original desapareció, cambió su base de datos o no puede volver a verificarse.
La copia completa del PBIP/PBIX, los visuales y el guardado/reapertura del proyecto
siguen fuera del alcance de este respaldo de objetos.

Archivos de actualización:

- Obligatorio para ejecutar: **cliente.py**.
- Si usas transferencia codificada: **codigo.txt**, regenerado, o vuelve a ejecutar
  codificar.py. Los scripts codificar.py/decodificar.py no cambiaron.
- Para documentación y pruebas: README.md y tests/ del paquete actualizado.
- No copies backups/ del modelo de prueba a la otra laptop como configuración.


## Inicio rápido en Windows 11

1. Extrae todos los archivos en una carpeta autorizada.
2. Abre un PBIX en Power BI Desktop y comprueba que está instalada la extensión
   **Power BI Authoring MCP Server** en Visual Studio Code.
3. Abre PowerShell en la carpeta que contiene `cliente.py`.
4. Si has revisado y aceptado la licencia del servidor, configura su aceptación
   para esa sesión y ejecuta el cliente:

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python cliente.py
```

La variable se hereda en el proceso del servidor. El cliente no la establece
automáticamente ni añade opciones para aceptar la licencia por su cuenta.

Elige el modelo por número. `2` lista tablas directamente; `3` lista medidas con
su tabla; `10` consulta relaciones; `11` ejecuta consultas DAX.

Para edición:

```powershell
python cliente.py --write
```

Con --write no se pide EDITAR al iniciar. Guarda una copia del PBIX. La opción 13
valida y ejecuta sin confirmaciones; los formularios 4 a 9 mantienen su confirmación. Los formularios de las opciones 4 a 9 envían los objetos
individualmente; la opción 13 incorpora la protección descrita más abajo. Se informa el resultado
individual y se conservan los cambios que ya fueron confirmados si otro falla.
El servidor puede solicitar además su propio consentimiento.

La detección busca las carpetas de extensiones de VS Code en el perfil del usuario
y ordena las versiones numéricamente. No contiene un puerto ni una ruta personal
fijos. Si la instalación corporativa usa otra ubicación autorizada:

```powershell
python cliente.py --server "D:\Herramientas\PowerBI\powerbi-modeling-mcp.exe"
```

## Uso de los menús

| Opción | Comportamiento |
| --- | --- |
| 1 | Detectar instancias y cambiar el modelo activo |
| 2 | Listar tablas; opcionalmente seleccionar una para ver su estructura |
| 3 | Listar medidas; seleccionar por número para ver DAX, descripción y carpeta |
| 4 / 7 | Crear una o varias medidas / columnas calculadas, seleccionando tablas |
| 5 / 8 | Seleccionar objetos, ver sus expresiones y preparar cambios con diferencias |
| 6 / 9 | Seleccionar objetos con `1,3`; confirmar eliminación sin cascada |
| 10 | Mostrar relaciones y solo los atributos devueltos por el servidor |
| 11 | Introducir DAX multilínea y opcionalmente guardar resultados en CSV |
| 12 | Inspeccionar herramientas, esquemas y respuestas completas; ejecutar JSON avanzado |
| 13 | Pegar y ejecutar un bloque JSON de instrucciones generado por IA |
| 0 | Cerrar el cliente y su proceso MCP |

En la lista de medidas, `/Ventas` filtra por nombre o tabla; `/` recupera toda la
lista. El filtro no distingue mayúsculas. Enter permite regresar.

Las expresiones y consultas terminan con una línea que contenga `FIN`. Al editar,
`FIN` sin expresión conserva la existente. En descripción, carpeta y formato,
Enter conserva el valor y `/vaciar` lo borra. No se cambia la tabla asignada a una
medida mediante Update. Las opciones normales de columnas solo modifican o
eliminan columnas calculadas DAX cuyo tipo pueda confirmarse; omiten columnas
importadas, de tablas calculadas y de tipo desconocido.

La opción DAX utiliza exclusivamente `dax_query_operations.Execute` y pide
resultados `Inline` cuando el esquema lo admite. Devuelve hasta 1000 filas y
advierte si se alcanza ese límite. El CSV conserva los valores completos; la
pantalla limita cada celda a 60 caracteres. No sobrescribe archivos existentes.

La opción avanzada conserva el ingreso manual de JSON del cliente original.
Sus operaciones de modelo usan explícitamente la conexión activa y rechazan un
identificador diferente. Una operación avanzada que cambie conexiones invalida
la selección del menú y obliga a elegir nuevamente el modelo.

## Respaldo y recuperación de bloques (opción 13)

La opción 13 conserva el orden y ahora usa --write como autorización, respalda el estado
previo y comprueba la recuperación cuando falla el bloque. Las opciones 1 a 12
siguen disponibles; los formularios 4 a 9 y el modo avanzado conservan su
funcionamiento anterior. El diario y la recuperación global descritos aquí
protegen los bloques de la opción 13.

### Respaldo antes de escribir

Se crea `backups/bloque_FECHA_HORA_IDENTIFICADOR.json` en la carpeta de ejecución.
Incluye el modelo y su conexión original, las instrucciones, el estado completo
devuelto por Get para cada objeto afectado, exportaciones TMDL cuando el servidor
las permite y un diario guardado antes de cada escritura. Se escribe de forma
atómica y se fuerza su guardado en disco. Si el respaldo inicial no puede
guardarse, el cliente no envía las escrituras del bloque.

Puedes elegir otra carpeta local autorizada:

```powershell
python cliente.py --write --backup-dir "D:\RespaldosPowerBI"
```

El respaldo contiene metadatos y fórmulas del modelo: consérvalo según las reglas
de tu organización. No se publica en GitHub. No es una copia del archivo PBIX o
del proyecto PBIP, de sus datos, visuales, filtros o archivos de informe.

### Dos vías de protección comprobadas

1. **Bloque solo de escrituras:** si el esquema del servidor ofrece Begin, Commit
   y Rollback, se abre una transacción. Cada resultado individual se muestra como
   verificado y pendiente de Commit. Al terminar se confirma y se vuelve a leer el
   estado. Si falla una instrucción, se solicita Rollback y se compara cada objeto
   afectado con su respaldo original.
2. **Bloque con escrituras y consultas DAX, o servidor sin transacciones:** se
   aplican las instrucciones en orden y se usa recuperación por compensación en
   caso de fallo. Se recorre el diario en orden inverso: se borran creaciones, se
   restauran propiedades anteriores y se recrean objetos eliminados. Se verifican
   después todos los objetos respaldados. Esta vía no es atómica y otros clientes
   pueden ver cambios intermedios.

La segunda vía es necesaria en este servidor: las consultas DAX no ven medidas
nuevas pendientes de una transacción de modelado. Se comprobó con el ejecutable
de la extensión instalada; no se cambia silenciosamente el orden para resolverlo.

El cliente detecta estados y mensajes de error semántico después de escribir o
confirmar. Una expresión presente en Get no basta para anunciar éxito: el servidor
puede guardar una fórmula inválida y marcar el objeto con SemanticError. Si esto
se descubre después de Commit, se intenta compensar desde el respaldo porque la
transacción ya está cerrada.

### Cómo leer el resultado

- `Estado final del bloque: completado`: las operaciones terminaron y, cuando
  correspondía, se confirmó la transacción y se verificó el estado posterior.
- `Estado final del bloque: fallido`: hubo un error de operación o de confirmación
  final. Los contadores de operaciones anteriores no significan que sus cambios
  se hayan conservado.
- `RECUPERACION VERIFICADA`: los objetos afectados coinciden con sus estados
  respaldados. Se normalizan valores vacíos equivalentes y no se exige recuperar
  marcas de tiempo o estados derivados. Se comprueba que no aparezcan nuevos
  errores semánticos. No significa restauración completa de todo el PBIP/PBIX.
- `RECUPERACION INCOMPLETA / NO VERIFICADA`: quedan diferencias, errores o no se
  pudo consultar con certeza la conexión original. Se muestra la ruta del respaldo
  y el archivo registra los errores. No se anuncia reversión completa.

Si Rollback no puede cerrar la transacción, el cliente no envía compensaciones
dentro de esa transacción incierta. Una conexión perdida solo se reutiliza cuando
se puede verificar la misma instancia original; no se cambia a otro modelo.
No hay recuperación automática al reiniciar el programa después de un cierre
forzado: el diario queda disponible para revisión. Sigue siendo recomendable una
copia del proyecto y evitar cambios concurrentes desde otras aplicaciones.

### Dependencias comprobadas por el motor

La opción 13 ejecuta `EVALUATE INFO.CALCDEPENDENCY()` mediante la herramienta DAX
existente. En el modelo de prueba devolvió inicialmente 457 registros. Se
contrastan referencias del motor con las eliminaciones y las modificaciones
previas del bloque; también se conserva el análisis local para las medidas nuevas
que todavía no existen en el motor. Si la consulta no está disponible en otra
versión, los formatos cambian o se alcanza el límite de 100000 filas, se indica
expresamente que la comprobación es parcial o no estuvo disponible.

El servidor semántico no permite garantizar todos los usos en visuales, filtros,
marcadores y otros objetos de un informe. Ese ámbito se advierte expresamente
antes de la confirmación; no se presenta como verificado.

### Pruebas de esta mejora, realizadas en el modelo abierto

El cliente utilizó el ejecutable de la extensión de VS Code de este equipo. Las
tablas se eligieron del catálogo real y los nombres de prueba fueron únicos;
ninguna tabla ni medida del programa depende de los nombres particulares de BI.

| Prueba real | Resultado |
| --- | --- |
| Crear 50 medidas en un bloque | 50 verificadas; Commit y lectura posterior correctos |
| Modificar 50 medidas en un bloque | Expresión, descripción y formato aplicados y verificados |
| Eliminar 20 medidas con sintaxis abreviada | 20 ausencias verificadas |
| Bloque mixto de 20 operaciones | Crear, modificar, eliminar y 5 consultas DAX correctas |
| Dependencias entre medidas | Detectadas por el motor y por el cliente; bloqueo de eliminación de la base |
| Quitar dependencia y eliminar la base | Orden respetado y cambios verificados |
| Error intermedio con transacción | Fallo MCP intencional en el paso 4; paso 5 no enviado; Rollback restauró lo anterior |
| Error intermedio sin transacción | Fallo DAX en el paso 4; recuperación por compensación verificada |
| Fórmula DAX inválida | SemanticError detectado después de Commit y recuperación compensatoria verificada |
| Sesión MCP independiente | Vio el valor guardado 200 y lo evaluó con DAX |
| Recuperación desde otro proceso MCP | Comprobó metadatos y valores DAX 1 y 2 tras Rollback, compensación y SemanticError |
| Limpieza final | No quedaron objetos generados por la prueba |

Para provocar un error real del MCP durante la transacción, la prueba inyecta una
consulta DAX inválida antes del cuarto cambio. No simula la respuesta: el error
lo devuelve el servidor real. La consulta inválida del bloque sin transacción y
la fórmula inválida también se enviaron realmente al servidor.

Durante las pruebas se corrigieron tres problemas reales: confundir el cambio de
tipo inferido Unknown a Int64 al confirmar con un fallo; consultas que no veían
medidas pendientes de Commit; y fórmulas inválidas guardadas como SemanticError.
También se separó el estado final del bloque de los contadores individuales para
que un fallo de Commit no aparezca como éxito global.

La versión anterior de recuperación pasó **98 pruebas automatizadas**, incluidas actualmente en las 135: casos de respaldo,
transacción, compensación, errores de guardado, cierre/confirmación fallidos,
recuperación incompleta, inferencia de tipo y errores semánticos. Los casos de
disco lleno o servidor inaccesible son simulados; no se afirma haber provocado
esas averías físicas en el equipo. No se probó guardar y reabrir el PBIP/PBIX, ni
restaurar informes, datos, identidades de objetos complejos o todas las versiones
del servidor. La compensación se comprobó con las propiedades de las medidas de
prueba, no con todos los KPI, traducciones u otros metadatos posibles.

Pruebas normales (sin escrituras reales):

```powershell
python -m unittest discover -s tests -v
```

Pruebas reales extensas, únicamente sobre un modelo destinado a pruebas:

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python tests/live_recovery.py --modelo "Titulo del modelo" --confirmar-escrituras
```

El informe se guarda en `resultado_recuperacion.json`. Los respaldos y los informes
locales se excluyen de Git. `codificar.py` se conserva y `codigo.txt` se regenera
para transferir el cliente actualizado.


## Opción 13: ejecutar un bloque generado por IA

La IA solo prepara el JSON. El cliente no llama a ChatGPT, Codex ni a otro servicio
para ejecutar el bloque. Se mantienen las opciones 1 a 12 y su transporte MCP.

1. Inicia con `python cliente.py --write` si habrá cambios.
2. Selecciona el modelo por número. Ese será el único destino del bloque.
3. Selecciona `13`, pega el JSON y escribe `FIN` en una línea independiente.
4. El programa valida todo el bloque antes de escribir. Si alguna instrucción es
   inválida, informa el problema y no ejecuta el lote.
5. Imprime el resumen y ejecuta automáticamente, sin CONFIRMAR ni s/N. Con --debug
   muestra las expresiones y propiedades completas sin preguntar.
6. Muestra resultados y estado de recuperación. Regresa al menú sin pedir Enter.

Ejemplo individual:

```json
{"action":"create_measure","table":"Medidas","name":"Prueba","expression":"1 + 1"}
```

Ejemplo mixto, con orden explícito:

```json
{
  "operations": [
    {"action":"create_measure","table":"Medidas","name":"Prueba","expression":"1"},
    {"action":"update_measure","table":"Medidas","name":"Prueba","expression":"2"},
    {"action":"execute_dax","query":"EVALUATE ROW(\"Valor\", [Prueba])"},
    {"action":"delete_measure","table":"Medidas","name":"Prueba"}
  ]
}
```

**Medidas es un nombre ilustrativo**: sustituye las tablas y los nombres por los
de tu modelo. Crear exige que el nombre no exista; modificar y eliminar exigen
que el objeto exista en ese punto de la secuencia. Un objeto creado al principio
puede modificarse y eliminarse después en el mismo bloque.

### Acciones permitidas

| action | Campos obligatorios adicionales | Opcionales |
| --- | --- | --- |
| `create_measure` / `create_column` | `table`, `name`, `expression` | Propiedades compatibles |
| `update_measure` / `update_column` | `table`, `name` y al menos una propiedad modificable | `expression` y propiedades compatibles |
| `delete_measure` / `delete_column` | `table`, `name` | Ninguno |
| `delete_measures` | `table`, `names` (lista no vacía) | Ninguno |
| `execute_dax` | `query` | `maxRows`, entre 1 y 100000; predeterminado 1000 |

Las propiedades de creación/actualización admitidas por este formato son
`description`, `displayFolder`, `formatString`, `isHidden` (booleano) y `dataType`.
Se comprueba además que el esquema real permita cada propiedad. `dataType` puede
tener restricciones del servidor para ciertas medidas o columnas. Una cadena
vacía en descripción, carpeta o formato solicita borrar el valor. No se admiten
propiedades ni acciones ajenas a esta lista; el formato no permite ejecutar código
Python, PowerShell, comandos del sistema ni solicitudes MCP arbitrarias.

No se requieren ni se aceptan `model`, `version`, `connectionName` ni cadenas de
conexión en el JSON. La conexión se toma del estado del cliente. El bloque puede
ser una operación individual o un objeto con solo `operations`. También puedes
colocar `delete_measures` dentro de `operations`; se expande en el lugar indicado,
respetando el orden de `names`:

```json
{"action":"delete_measures","table":"Medidas","names":["Medida B","Medida A"]}
```

Para tablas diferentes, incluye una operación por tabla o varios `delete_measures`.
No hay búsqueda parcial, coincidencias por prefijo ni expansión de comodines.

### Validación, dependencias y errores

- El catálogo virtual reproduce el orden indicado, valida tablas y objetos,
  nombres de medidas únicos en el modelo, columnas calculadas compatibles,
  campos y esquemas de tools/list. Las eliminaciones desactivan la cascada.
- Se rechazan instrucciones idénticas, dos acciones iguales sobre el mismo objeto,
  actualizaciones sin cambios especificados y recreaciones tras eliminar el mismo
  objeto. Una secuencia crear, modificar y eliminar sí está permitida y se avisa.
- Se buscan posibles referencias DAX entre objetos, referencias a objetos creados
  después o eliminados antes y ciclos entre medidas. Antes de eliminar, se leen
  expresiones existentes; una dependencia detectada exige modificar o eliminar
  primero al dependiente. Para columnas se comprueban también las relaciones.
- El análisis es léxico y conservador, no un compilador DAX. Puede advertir o
  bloquear referencias ambiguas y no cubre todas las dependencias de visuales,
  grupos de cálculo, metadatos o consultas. Los detalles de columnas sin tabla y
  nombres repetidos pueden dejar el análisis parcial; se informa. El servidor
  mantiene la validación definitiva de las expresiones y de las dependencias.
- No se reordenan instrucciones ni se envía un lote parcialmente válido. La
  validación de un modelo que cambie simultáneamente desde otra aplicación puede
  quedar desactualizada: revisa ese caso y no ejecutes cambios concurrentes.
- Tras cada escritura se consulta el modelo con List/Get para verificar el estado,
  incluso si el servidor devuelve contenido vacío. No se anuncia éxito por el solo
  hecho de haber enviado una solicitud.
- Al primer fallo, resultado incierto o pérdida de conexión, el lote se detiene
  e intenta recuperar el estado respaldado. El resumen distingue operaciones,
  resultado global y recuperación verificada o incompleta. No se reintentan las
  instrucciones originales a ciegas. Consulta la sección de respaldo y recuperación.

No hay límite de cinco, diez ni cien operaciones. Una prueba simulada ejecuta
300 operaciones. El tamaño práctico depende de memoria, tiempos del servidor y
del modelo. El inventario usa el límite de 100000 objetos del cliente existente y
advierte al alcanzarlo; no se garantiza inventario completo por encima de ese
valor. La lista de acciones solo se ampliará cuando se comprueben las herramientas
y esquemas necesarios; no hay creación o eliminación de tablas o relaciones en
esta versión del formato.

### Ejemplos listos para adaptar

- `examples/crear_medidas.json`: dos medidas; la segunda depende de la primera.
- `examples/modificar_medidas.json`: modificación de expresiones y propiedades.
- `examples/eliminar_medidas.json`: eliminación abreviada, dependiente primero.
- `examples/bloque_mixto.json`: crear, modificar, consultar y eliminar.
- `examples/columnas_calculadas.json`: ciclo completo de columnas DAX.

Copia el contenido de un archivo, no su ruta, en la opción 13. Termina con FIN.
Para pedirle un bloque a una IA puedes usar:

> Genera solo JSON para mi opción 13. Usa operations y estas acciones:
> create_measure, update_measure, delete_measure, delete_measures, create_column,
> update_column, delete_column y execute_dax. No incluyas model, version ni
> connectionName. Conserva el orden de dependencias y usa únicamente estas tablas,
> medidas y columnas existentes: [pega aquí sus nombres]. Estas son mis instrucciones:
> [describe aquí los cambios].

### Comprobación de esta modificación

```powershell
python -m unittest discover -s tests -v
python codificar.py cliente.py codigo.txt
```

La primera versión de bloques pasó **84 pruebas automatizadas**, incluidas las 51 de regresión del cliente
anterior y 33 nuevas para bloques. La versión actual pasa 135. Se cubren creación individual, 300 creaciones,
modificaciones y eliminaciones múltiples, mezcla de acciones, tablas diferentes,
JSON inválido y claves duplicadas, listas permitidas, conflictos, conexiones,
confirmación única, dependencias, columnas importadas, respuestas MCP vacías,
fallos parciales y tiempos de espera.

También se ejecutó realmente en `BI` un bloque de **11 operaciones: 10 escrituras
y una consulta DAX**. Se crearon dos medidas y dos columnas temporales, se
modificaron una medida y una columna y se eliminaron los cuatro objetos. La
consulta intermedia devolvió 10 y 11 para las medidas relacionadas. Todas las
escrituras se verificaron por lectura y el inventario final coincidió con el
inicial. Los objetos temporales quedaron eliminados. No se guardó ni reabrió el
PBIX. El consentimiento interactivo adicional del servidor sigue cubierto por
simulaciones; no se solicitó durante esta prueba real.

Para repetir **solo si autorizas nuevas escrituras temporales**:

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python tests/live_ai_block.py --modelo "BI" --confirmar-escrituras
```

El script registra un informe local `resultado_bloque_ia.json`, que no se incluye
en el control de versiones. Las pruebas normales unittest nunca escriben en un
modelo real. El generador de código codificado y su decodificador se conservan;
`codigo.txt` se regenera a partir del cliente actualizado y es reversible byte a byte.


## Cambios respecto del archivo original

Se mantuvieron `MCP`, subprocess con stdin/stdout, lectura con hilos y cola,
initialize, tools/list, tools/call, detección de instancias, selección numérica,
conexión con la cadena detectada, menú de 12 opciones y modo avanzado. También
se conservan `field_value`, `collect_fields`, `pretty`, `tool_schema` y los puntos
de entrada de los menús. No se sustituyó el transporte por otro proyecto.

Los cambios principales son:

- Las consultas normales construyen sus solicitudes sin formularios de esquema.
- Connect nunca recibe el identificador de la conexión anterior y acepta tanto
  `data: "identificador"` como un objeto con `connectionName`.
- Se preserva `tableName` cuando List agrupa medidas o columnas por tabla. Este
  formato se observó en el servidor real y es necesario para Get y la edición.
- Se usan `definitions` para Create/Update y `references` para Get/Delete,
  comprobando las propiedades del esquema descubierto. Se resuelven referencias
  JSON locales y envoltorios anulables compatibles. Si un formato nuevo es ambiguo,
  se informa en lugar de adivinarlo.
- tools/list admite paginación. Las operaciones se contrastan con enum o con la
  descripción declarada por el servidor. Los filtros solicitan hasta 100000 objetos;
  alcanzar el límite provoca una advertencia.
- La identidad de la conexión se comprueba antes de operar, incluyendo puerto,
  título y, si están disponibles, proceso y hora de inicio. No se reconecta ni se
  reintenta una escritura automáticamente tras una respuesta incierta.
- Se bloquean escrituras en el cliente y se inicia el servidor con `--readonly`
  por defecto. También se aplica el bloqueo al modo avanzado.
- Se interpretan errores MCP, contenido JSON anidado, contenido estructurado y
  recursos de texto embebidos. Si una escritura devuelve contenido vacío o carece
  de confirmación explícita, se consulta List/Get para verificar su estado real:
  propiedades guardadas para Create/Update y ausencia para Delete. No se anuncia
  éxito si esa comprobación falla.
- El plazo de espera es absoluto y no se reinicia por notificaciones. Se responden
  ping y solicitudes estándar de consentimiento `elicitation/create`; estas últimas
  requieren aprobación interactiva. Se cierra el proceso incluso si falla el inicio.
- Los mensajes rutinarios `IsError=False` no ensucian la consola. `--debug` muestra
  las respuestas completas y la salida de diagnóstico del servidor.

Las validaciones locales cubren el subconjunto de JSON Schema necesario para las
solicitudes generadas; el servidor sigue siendo la autoridad para las reglas de
negocio, las expresiones DAX y los cambios de modelo. No es un validador general
de todas las variantes posibles de JSON Schema.

## Transferencia con indentación reversible

Usa únicamente canales de transferencia autorizados por tu organización.

```powershell
python codificar.py cliente.py codigo.txt
python decodificar.py codigo.txt cliente_reconstruido.py
```

Cada línea usa `numero_de_espacios|contenido`. Se trabaja en bytes UTF-8, sin
normalizar LF, CRLF, BOM, tabuladores, líneas vacías, espacios finales ni la
ausencia del salto final. Solo los espacios iniciales se reemplazan por el número;
los tabuladores permanecen literales. El texto deja de ser reversible si el canal
de transferencia altera esos bytes. `decodificar.py` rechaza formatos inválidos y
no sobrescribe archivos salvo que añadas `--sobrescribir`.

Comprobación byte a byte en PowerShell:

```powershell
(Get-FileHash cliente.py).Hash -eq (Get-FileHash cliente_reconstruido.py).Hash
```

## Pruebas y resultados del 21 de noviembre de 2026

Pruebas automatizadas sin Power BI ni dependencias externas:

```powershell
python -m unittest discover -s tests -v
```

Las 51 pruebas iniciales de regresión se conservan dentro de las 135 actuales. Simulan selección y cambio de modelos, respuestas de conexión en ambos
formatos, conexiones cerradas y puertos reutilizados, lectura, filtros, estructuras
MCP anidadas, solicitudes con los esquemas reales, escritura bloqueada,
confirmaciones, edición y eliminación múltiple, errores parciales, plazos,
consentimiento, columnas calculadas/importadas, CSV y transferencia byte a byte.
`tests/tool_schemas.json` contiene seis esquemas capturados de tools/list del
servidor instalado 1.0.1; las respuestas del modelo usadas por unittest son sintéticas.

Prueba real opcional que solo consulta el modelo:

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python tests/live_powerbi.py --modelo "Titulo del modelo"
```

Se ejecutó realmente con Python 3.12.10, servidor 1.0.1 y el modelo abierto `BI`:
detección de instancia, Connect, GetConnection, 9 tablas, 58 medidas, 100 columnas,
8 relaciones, Get de tabla/medida/columna, visualización de los menús de lectura,
una consulta `EVALUATE ROW("PruebaCliente", 1)` y detección de un error DAX deliberado.
En una prueba posterior, autorizada expresamente sobre el mismo modelo `BI`, se
comprobaron realmente Create, Update y Delete de dos medidas y dos columnas
calculadas temporales en la tabla `Selector Escenario`: 12 escrituras verificadas
mediante lectura posterior. Update cambió expresiones y descripciones. Se
eliminaron todos los objetos temporales y se comprobó que los inventarios de
medidas, columnas y tablas, y las relaciones, coincidían con los iniciales.

Esta prueba detectó que el servidor 1.0.1 puede responder a escrituras con
`content: []` e `isError: false` aunque sí aplique el cambio. El cliente ahora
verifica el resultado mediante List/Get antes de anunciar éxito. El primer
intento detectó esa respuesta ambigua, se detuvo y limpió sus medidas temporales;
la ejecución completa posterior validó ambas clases de objetos. No se guardó ni
reabrió el PBIX: la persistencia en disco queda pendiente de validación. La
interacción de consentimiento se cubre con simulaciones; el servidor no la pidió
durante estas escrituras reales.

El informe de la ejecución completa está en `resultado_escrituras.json`.
El script de escritura es **opcional** y solo se ejecuta con autorización explícita:

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python tests/live_write_powerbi.py --modelo "BI" --confirmar-escrituras
```

El script crea nombres únicos, comprueba cada cambio, elimina sus objetos y
registra la limpieza incluso si falla una etapa. El argumento autoriza los
objetos temporales y los consentimientos del servidor que correspondan a esa
prueba. No se prometen operaciones compatibles con todas las versiones futuras.

## Documentación oficial consultada

- [Repositorio y documentación de Microsoft](https://github.com/microsoft/powerbi-modeling-mcp):
  herramientas, modos de acceso, aceptación de licencia y limitaciones. El
  repositorio público consultado distribuye documentación y binarios; los detalles
  de los esquemas se comprobaron directamente mediante tools/list del ejecutable.
- [Especificación MCP: consentimiento interactivo](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation).

El servidor respeta permisos y restricciones de Power BI. Sus operaciones siguen
las reglas de herramientas externas, y puede solicitar consentimiento adicional.
No se cambian políticas corporativas ni se instalan componentes por el cliente.
