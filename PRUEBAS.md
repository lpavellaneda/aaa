# Validacion ejecutada el 22 de noviembre de 2026

Base: `Sheccid1/Respuesta/cliente.py`, SHA de archivo
`2178a570cf4022ffce2034f852db7a0954c7f0fd`.

## Pruebas automatizadas

`python -m unittest discover -s tests -t .`: **151 pruebas correctas**.
Incluye las 135 pruebas anteriores y 16 nuevas de exportacion y modo forzado.
Son simulaciones del protocolo, no equivalen por si solas a pruebas sobre Desktop.

Las nuevas cubren propiedades desconocidas del MCP y formatos sin recorte,
exportacion parcial explicita, archivo UTF-8, detalle mostrado una sola vez,
exportacion desde los menus 2/3, tipo booleano de force, eliminaciones multiples,
advertencias de dependencias, autorizacion obligatoria, cancelacion sin escrituras,
modo lectura, falta de transacciones, objetos inexistentes, proteccion de columnas
de origen, columnas con relaciones y deteccion de cambios colaterales/errores.

## Pruebas reales con la extension instalada

Se uso el ejecutable original de VS Code, version 1.0.1, sobre el modelo de prueba
BI abierto por el usuario en esta PC. Se selecciono la instancia descubierta por
el MCP; el puerto y el nombre de tablas no quedaron fijados en el cliente.

`tests/live_export_force.py`: **9 escenarios correctos**:

1. Exportacion de 9 tablas, 58 medidas, 100 columnas, 20 columnas calculadas y 8 relaciones.
2. Conservacion de expresion, formato numerico, carpeta y descripcion; expresion de columna calculada incluida.
3. La eliminacion normal sigue bloqueando dependencias antes de escribir.
4. Eliminacion forzada de una medida utilizada por otra, verificada desde un proceso MCP nuevo.
5. Fallo intencional intermedio tras forzar una eliminacion: rollback y verificacion del inventario y TMDL previos.
6. Bloque mixto con creacion, actualizacion y eliminacion de medidas y columnas calculadas.
7. Eliminacion forzada de columna calculada.
8. Fallo intermedio tras eliminar una columna con relacion y expresiones dependientes: rollback restauro tambien la relacion, verificado desde otro proceso MCP.
9. Eliminacion forzada de columna con relacion: desaparecieron la columna y la relacion, comprobadas mediante un proceso MCP nuevo.

Los fallos intermedios de estos escenarios se inyectaron en la llamada del cliente
despues de que el MCP real ya hubiera aplicado la eliminacion dentro de una
transaccion. No se simulo una perdida real de Power BI en estos nuevos escenarios.

`tests/live_ai_block.py --modelo BI --confirmar-escrituras`: **11 operaciones
correctas**, 10 escrituras y una consulta. Incluyo dos medidas, dos columnas,
actualizaciones, consulta DAX y eliminaciones; no aparecieron confirmaciones
adicionales en el bloque normal con --write.

Se eliminaron todos los objetos generados para las pruebas y se verifico que el
inventario final completo coincidiera con el inicial. Las pruebas no se ejecutaron
en la otra laptop.

## Errores corregidos y comportamiento comprobado

- El menu de medidas contenia dos recorridos de seleccion/detalle copiados;
  ahora muestra una sola vez el objeto seleccionado.
- La exportacion anterior reducia cada medida a seis propiedades; ahora conserva
  las propiedades disponibles en el MCP, informa errores por objeto y guarda JSON.
- Las dependencias ya no impiden una eliminacion explicitamente forzada, pero
  siguen bloqueando las eliminaciones normales.
- Una ausencia de capacidad de transaccion produce ahora un error tecnico claro
  antes de escribir en modo forzado.
- La recuperacion de un bloque forzado no se declara verificada comprobando solo
  los objetos solicitados: se compara ademas la captura completa y el TMDL.

## Limites comprobados

El servidor puede conservar medidas y columnas dependientes con `SemanticError`
tras una eliminacion forzada. El cliente avisa; no inventa formulas de reemplazo.
Una cascada puede borrar relaciones. Los usos en visuales/filtros del informe no
se pueden verificar por completo con estas herramientas del modelo semantico.

Se probo rollback nativo antes de Commit con medidas, columnas y una relacion.
No se afirma recuperacion completa de toda cascada posible despues de Commit,
ni despues de cerrar Desktop, ni restauracion completa de un PBIX/PBIP. La
compensacion anterior sigue intentando restaurar los objetos explicitos; las
diferencias colaterales sin resolver producen recuperacion incompleta. Los
respaldos son metadatos locales, no una copia de datos o del informe completo.

El inventario exporta todas las propiedades que expone el MCP, no propiedades
internas que el servidor no entregue. Para forzar se exigen transaccion y TMDL;
las consultas execute_dax deben ir en otro bloque.
