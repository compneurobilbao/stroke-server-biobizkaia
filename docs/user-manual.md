# Manual De Usuario

1. Entra en la GUI.
2. Inicia sesión con tu correo electrónico y contraseña.
3. Confirma que la imagen está anonimizada.
4. Selecciona una imagen anatómica T1w en formato `.nii.gz`.
5. Introduce un identificador BIDS de sujeto, por ejemplo `sub-O01`. Si lo dejas vacío, el sistema genera uno seguro.
6. Pulsa `Enviar a procesamiento`.
7. Espera a que el estado pase de `En cola` a `Procesando` y luego a `Completado`. Puede tardar entre 10 minutos y 1 hora.
8. Descarga el PDF técnico y, si aparece, el ZIP de resultados.
9. Si necesitas compartir el PDF con alguien sin cuenta, usa `Compartir` para crear un enlace temporal y revocable.

Si el estado es `Fallido`, avisa al administrador y no uses ese resultado.

El PDF técnico lista metadatos, ficheros generados y PNG renderizados automáticamente desde resultados NIfTI mediante FSL `slicer`. No es un informe clínico validado y no debe usarse para tomar decisiones médicas sin revisión profesional.

Si el estado aparece como completado con avisos, el procesamiento principal terminó pero algún NIfTI pudo no renderizarse a PNG. En ese caso revisa el PDF y avisa al administrador si faltan imágenes esperadas.

## Gestión De Estudios

Desde la tabla de estudios se puede, según rol y estado del estudio:

- Ver detalle técnico del estudio y sus trabajos.
- Ver logs truncados si existen.
- Cancelar estudios en cola o solicitar cancelación si ya están procesando, cuando la acción está disponible en la GUI.
- Reintentar estudios fallidos.
- Borrar estudios que no están procesando.
- Compartir el PDF técnico de estudios completados mediante enlaces temporales.
- Marcar resultados como `Solo técnico`, o como `Revisado`/`Validado` cuando están completados, para trazabilidad interna.

El borrado elimina físicamente los ficheros asociados y deja auditoría mínima en base de datos.

## Compartición De Informes

En estudios `Completado` con PDF disponible, el botón `Compartir` permite crear un enlace temporal. Ese enlace permite descargar solo el PDF técnico, no el ZIP ni los logs, y no requiere crear una cuenta para el receptor.

Los enlaces caducan automáticamente y pueden revocarse desde la misma pantalla. Si se pierde un enlace ya creado, no se puede recuperar porque el sistema no guarda el token en claro; crea uno nuevo y revoca el anterior si corresponde.

## Usuarios Y Roles

- `researcher`: puede subir estudios desde la GUI, ver su historial propio, consultar su cuota, descargar resultados propios, gestionar enlaces PDF de sus estudios y marcar revisión técnica de sus resultados.
- `admin`: puede ver todos los estudios, crear/desactivar usuarios, definir cuotas, consultar el dashboard operativo, borrar estudios permitidos, gestionar enlaces PDF y marcar revisión técnica. El panel admin no expone subida, cancelación/retry ni ejecución de pipelines.

No hay registro público abierto. Los usuarios se crean desde una cuenta admin.

## Administración Básica

Para crear el primer admin, usar:

```bash
make create-admin EMAIL=admin@example.org
```

Después, el admin puede crear usuarios `researcher` o `admin` desde la GUI, asignar cuotas de almacenamiento y desactivar/borrar usuarios de forma lógica.

El dashboard admin muestra cola, jobs fallidos, servicios, uso de disco, usuarios y estudios por estado. Sirve para diagnóstico operativo; no sustituye la revisión de logs si un procesamiento falla.

Quedan para fases posteriores la subida múltiple, una pantalla GUI de retención y la revisión clínica formal.
