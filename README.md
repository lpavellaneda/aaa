# Cliente MCP de Power BI

Esta actualizacion parte de `Sheccid1/Respuesta/cliente.py`, revision
`2178a570cf4022ffce2034f852db7a0954c7f0fd`. Conserva las funciones existentes.
Necesita Python 3.12 y el servidor Microsoft Power BI Modeling MCP de VS Code.
Solo usa la biblioteca estandar de Python.

```powershell
$env:PBI_MODELING_MCP_ACCEPT_EULA="true"
python cliente.py --write
```

Sin `--write` se mantiene el modo de lectura. El cliente encuentra el ejecutable
en las extensiones de VS Code del usuario actual, sin fijar un usuario ni un puerto.

## Exportar JSON para compartir con la IA

- Opcion **3**, escribir `json`: todas las medidas con sus propiedades completas.
- Opcion **2**, escribir `json`: metadatos completos, incluidas tablas y columnas.
- Opcion **14**: exportacion completa directamente.

El JSON aparece en consola y se guarda como UTF-8 en `exports/metadatos_*.json`.
Puedes compartir ese archivo para preparar las modificaciones de la opcion 13.
No son instrucciones ejecutables: el inventario exportado y el bloque de operaciones
tienen formatos diferentes, para evitar ejecutar por accidente una exportacion.

La exportacion completa contiene `tables`, `measures`, `columns`,
`calculatedColumns` y `relationships`, ademas del nombre del modelo. Los objetos
conservan todas las propiedades devueltas por List/Get, sin una lista reducida de
campos: `tableName`, `name`, `expression`, `formatString`, `displayFolder`,
`description`, `isHidden`, anotaciones y otras propiedades disponibles.
`calculatedColumns` incluye las columnas reconocidas como calculadas por el MCP;
`columns` incluye tambien las de origen para entender las dependencias.

Si falla una lectura se indica `complete: false`, se marca el objeto con
`metadataIncomplete` y se detallan los problemas en `errors`. No se presenta una
exportacion parcial como completa. No se exportan filas de datos. Las expresiones,
consultas y metadatos pueden contener informacion propia del modelo.

## Opcion 13: creacion, actualizacion y eliminacion

Admite `create_measure`, `update_measure`, `delete_measure`, `create_column`,
`update_column`, `delete_column` y consultas `execute_dax`. Las columnas creadas,
editadas o eliminadas por este menu deben ser calculadas; no se eliminan columnas
de origen. `delete_measures` permite abreviar una lista de nombres.

Pega un objeto JSON y termina con `FIN` en una linea independiente:

```json
{
  "operations": [
    {"action": "create_measure", "table": "Ventas", "name": "Total", "expression": "SUM('Ventas'[Importe])", "formatString": "#,0.00"},
    {"action": "update_measure", "table": "Ventas", "name": "Total", "description": "Venta total"},
    {"action": "create_column", "table": "Ventas", "name": "Doble", "expression": "'Ventas'[Importe] * 2"},
    {"action": "update_column", "table": "Ventas", "name": "Doble", "expression": "'Ventas'[Importe] * 3"},
    {"action": "delete_column", "table": "Ventas", "name": "Doble"},
    {"action": "delete_measure", "table": "Ventas", "name": "Total"}
  ]
}
```

Sustituye tablas y columnas por las del JSON de tu modelo. Con `--write`, un bloque
normal se valida y ejecuta sin preguntas adicionales. Conserva el orden, resultado
por operacion, verificaciones de identidad de conexion, respaldo y recuperacion.

## Eliminacion forzada explicitamente autorizada

El modo normal sigue deteniendose ante dependencias detectadas. Para autorizar
una eliminacion pese a ellas, incluye `"force": true` **solo en la eliminacion**:

```json
{
  "operations": [
    {"action": "delete_measure", "table": "Ventas", "name": "Medida antigua", "force": true},
    {"action": "delete_column", "table": "Ventas", "name": "Calculada antigua", "force": true}
  ]
}
```

Tras las advertencias, escribe exactamente `CONFIRMAR FORZAR` una sola vez para
ese bloque. Otra respuesta cancela el bloque completo sin escribir. Esta es la
unica confirmacion adicional de la opcion 13 y solo aparece si hay `force: true`.
`--write` sigue siendo obligatorio. Se permite mezclar esas eliminaciones con
creaciones y modificaciones de medidas/columnas.

El cliente envia `shouldCascadeDelete: true` al MCP exclusivamente para las
eliminaciones marcadas. No omite existencia, tipos, esquema, identidad del modelo,
errores de DAX ni comprobaciones de transaccion. Detecta y advierte dependencias,
pero estas ya no bloquean la eliminacion autorizada. Una operacion tecnicamente
imposible o rechazada por el servidor sigue deteniendo el bloque.

**Comportamiento comprobado:** forzar una medida base puede dejar una medida
dependiente con `SemanticError`; no reescribe automaticamente sus formulas.
Forzar una columna puede borrar relaciones. Se informa del estado posterior y
se guardan los efectos detectados en el respaldo. La eliminacion solicitada puede
ser correcta aunque un dependiente haya quedado invalido por esa decision.

El modo forzado exige transacciones Begin/Commit/Rollback y exportacion TMDL del
modelo. No admite `execute_dax` en el mismo bloque forzado: separa las consultas,
porque el MCP las ejecuta desde otra sesion que no ve la transaccion abierta.

Antes del primer cambio forzado se respalda el inventario completo de tablas,
medidas, columnas y relaciones, ademas del TMDL con sus hijos. Si ese respaldo
falla, no se escribe. Si falla una operacion se intenta rollback y se verifica
tambien ese alcance completo. Si el rollback no es posible, se conserva la
compensacion existente de los objetos indicados; cualquier diferencia colateral
en el inventario/TMDL impide informar recuperacion verificada.

No se garantiza reconstruir automaticamente todos los objetos eliminados por una
cascada despues de Commit, ni restaurar visuales de un informe. Se conserva el
respaldo y se informa `incompleta` si no puede comprobarse la restauracion. No se
ha eliminado la verificacion de conexion para permitir las eliminaciones forzadas.

## Pruebas

```powershell
python -m unittest discover -s tests -t .
```

Las pruebas `tests/live_*.py` usan Power BI real y pueden escribir. La nueva prueba
`tests/live_export_force.py` exige exactamente un modelo abierto, que debe ser
descartable. Crea objetos con un prefijo unico, prueba exportacion, CRUD mixto,
dependencias, cascada y rollback con fallo intermedio. Verifica tambien desde un
proceso MCP independiente y limpia sus objetos al terminar. No ejecutar contra un
modelo de produccion. Resultados de esta PC: ver `PRUEBAS.md`.

Para actualizar otra laptop basta reemplazar `cliente.py`. Los archivos
`codificar.py`, `decodificar.py` y `codigo.txt` son opcionales para transportar el
codigo como texto; las pruebas y ejemplos no se necesitan para ejecutar el cliente.
