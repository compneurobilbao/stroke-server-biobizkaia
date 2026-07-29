# Guía De Demostración Para Tribunal

Esta guía está pensada como apoyo operativo durante la defensa. No sustituye a la memoria: ordena qué mostrar, qué decir y qué evitar para demostrar el sistema en directo sin depender de que un procesamiento largo termine durante la sesión.

## Objetivo De La Demo

Demostrar que el proyecto no es solo una interfaz de subida, sino una plataforma trazable para procesamiento asíncrono de neuroimagen:

- GUI web para usuarios investigadores y administración.
- API FastAPI con permisos, validación y trazabilidad.
- Cola Celery/Redis para trabajos largos fuera del ciclo HTTP.
- Frontera `processor_adapter` con el procesador externo.
- Persistencia en PostgreSQL y `data/studies`.
- Artefactos técnicos: PDF, PNG renderizados y ZIP.
- Operación: dashboard, logs, backup, retención por CLI y notificaciones.

Mensaje central:

> El valor del TFM está en integrar, aislar y operar un pipeline externo de forma reproducible y trazable, no en reimplementar un algoritmo clínico.

## Límites Que Conviene Aclarar

- No usar datos reales ni identificativos durante la demo.
- El PDF generado es técnico, no clínico.
- La plataforma no es un producto sanitario certificado.
- La revisión `Solo técnico` / `Revisado` / `Validado` es trazabilidad interna, no validación clínica formal.
- La retención tiene dry-run disponible, pero el borrado real está desactivado por defecto con `RETENTION_ENABLED=false`.
- La ejecución real con `compneuro` puede tardar demasiado para una defensa; conviene tener un estudio ya completado.

## Preparación Previa

Ejecutar antes de la sesión:

```bash
make up
make migrate
make smoke
```

Comprobar accesos:

- GUI: `http://localhost`
- Swagger/OpenAPI: `http://localhost/api/docs`
- Mailpit: `http://localhost:8025`

Preparar datos y usuarios:

- Un usuario `admin` funcional.
- Un usuario `researcher` funcional.
- Un fichero `.nii.gz` anonimizado de prueba.
- Un estudio ya completado con PDF técnico disponible.
- Un estudio fallido o cancelable si se quiere enseñar retry/cancelación.
- Mailpit abierto si se van a enseñar correos locales.

Si falta el admin inicial:

```bash
make create-admin EMAIL=admin@example.org
```

## Guion Corto De 10-15 Minutos

### 1. Apertura

Explicación breve:

> Esta plataforma permite a un investigador subir un estudio anatómico T1w anonimizado, lanzarlo a una cola de procesamiento, aislar la ejecución del pipeline externo, registrar trazabilidad y descargar resultados técnicos. La administración puede supervisar usuarios, estado operativo, almacenamiento y mantenimiento.

Mostrar rápidamente:

- GUI.
- Swagger.
- Dashboard admin si ya está abierto en otra pestaña.

### 2. Login Y Separación De Roles

Entrar como `researcher`.

Mostrar:

- Login local.
- Historial de estudios propio.
- Acciones disponibles para investigador.

Frase útil:

> Los estudios tienen propietario. Un investigador trabaja sobre su historial, mientras que administración tiene visión global.

Después entrar como `admin` o reservarlo para el bloque de administración.

### 3. Subida De Estudio

Como `researcher`, subir un `.nii.gz` anonimizado.

Mostrar:

- Selección de fichero.
- Identificador BIDS, por ejemplo `sub-O01`.
- Envío a procesamiento.
- Estado inicial `queued` o `processing`.

Explicación:

> La API valida permisos, extensión, cuota, sanitiza el nombre, guarda el fichero, crea un `Study`, crea un `ProcessingJob`, registra auditoría y encola una tarea Celery. La petición HTTP no queda bloqueada por el procesamiento.

### 4. Cola Y Procesamiento Asíncrono

Mostrar el cambio de estado o un estudio ya en procesamiento.

Explicación:

> Redis actúa como broker de cola. Celery ejecuta el trabajo en un worker separado. El worker llama al `processor_adapter`, que es la frontera obligatoria con el procesador externo.

Frase clave:

> FastAPI no conoce los detalles internos del algoritmo. Solo coordina trazabilidad, permisos y artefactos.

### 5. Detalle Técnico, Logs Y Jobs

Abrir el detalle de un estudio.

Mostrar:

- Datos del estudio.
- Lista de jobs.
- Logs truncados.
- Estado final o error.
- Retry si el estudio está fallido.
- Cancelación si el estudio está en cola o procesamiento.

Explicación:

> La trazabilidad permite saber qué se intentó ejecutar, cuándo, con qué estado y qué logs produjo. Esto es esencial para investigación reproducible y mantenimiento.

### 6. Artefactos De Salida

Abrir un estudio completado.

Mostrar:

- PDF técnico.
- ZIP de resultados si está disponible.
- PNG renderizados desde NIfTI.

Explicación:

> El PDF resume artefactos técnicos y metadatos de procesamiento. No interpreta la imagen ni emite diagnóstico.

### 7. Revisión Técnica

Mostrar el cambio de estado de revisión si está disponible.

Estados:

- `Solo técnico`.
- `Revisado`.
- `Validado`.

Explicación:

> Esta marca no sustituye la revisión clínica formal. Sirve para trazabilidad interna del resultado dentro de la plataforma.

### 8. Compartición Temporal De PDF

En un estudio completado, crear un enlace temporal.

Mostrar:

- Creación del enlace.
- Apertura en ventana privada o pestaña nueva.
- Descarga del PDF sin iniciar sesión.
- Revocación si se quiere mostrar.

Explicación:

> La compartición usa tokens opacos temporales. Solo comparte el PDF técnico, no el ZIP ni los logs.

### 9. Notificaciones

Mostrar:

- Notificaciones internas en la GUI.
- Preferencias de notificación si están visibles.
- Mailpit en `http://localhost:8025`.

Explicación:

> Los correos no adjuntan ficheros pesados ni datos clínicos. Informan del resultado y enlazan a la plataforma. Si SMTP falla, el procesamiento principal no se marca como fallido.

### 10. Dashboard Admin

Entrar como `admin`.

Mostrar:

- Estado de cola.
- Jobs activos y fallidos.
- Estado de PostgreSQL, Redis y worker.
- Uso de disco.
- Usuarios.
- Estudios por estado.
- Alertas básicas.

Explicación:

> El dashboard da visibilidad operativa sin convertir el panel admin en un lugar desde el que ejecutar pipelines manualmente.

### 11. Gestión De Usuarios Y Cuotas

Desde admin, mostrar:

- Creación de usuario.
- Rol `admin` o `researcher`.
- Cuota de almacenamiento.
- Activación/desactivación o borrado lógico si se usa usuario de prueba.

Explicación:

> Las cuotas y roles preparan la plataforma para un uso compartido más controlado, aunque sin entrar todavía en identidad institucional OIDC/ORCID.

### 12. API Documentada

Abrir `http://localhost/api/docs`.

Mostrar:

- Endpoints principales.
- Esquemas de entrada/salida.
- Endpoints protegidos.

Explicación:

> La API queda documentada de forma automática con OpenAPI. Esto ayuda a validar contratos y facilita mantenimiento o integración futura.

### 13. Operación: Backup Y Retención

Mostrar comandos, no ejecutar restore en vivo.

```bash
make backup
make retention-dry-run DAYS=90
```

Explicación:

> El backup considera PostgreSQL y `data/studies` como unidad lógica. La retención se prueba primero en dry-run. El borrado real requiere activación explícita con `RETENTION_ENABLED=true`.

No ejecutar en directo salvo entorno descartable:

```bash
make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND
make retention-execute DAYS=90
make clean
```

## Plan B Si Algo Falla

Si el procesamiento no termina:

- Usar un estudio ya completado.
- Explicar que el valor arquitectónico está en la cola asíncrona y trazabilidad.
- Mostrar logs y estado del job.

Si Docker va lento:

- Enseñar Swagger, dashboard y estudio precalculado.
- Evitar reconstrucciones o comandos largos.

Si Mailpit no muestra correo:

- Mostrar notificaciones internas.
- Explicar que SMTP es auxiliar y no bloquea el procesamiento.

Si el fichero de prueba falla:

- Mostrar el estado `failed`.
- Abrir logs.
- Enseñar retry.
- Usar esa situación como ejemplo de trazabilidad de incidencias.

Si preguntan por diagnóstico clínico:

> El sistema no diagnostica. Genera artefactos técnicos trazables de procesamiento. La validación clínica formal queda fuera del alcance del TFM y requeriría profesionales, protocolo y análisis normativo.

## Preguntas Probables Del Tribunal

### ¿Por qué usar Celery y Redis?

Porque el procesamiento puede tardar minutos u horas. Sacarlo del ciclo HTTP evita timeouts, permite reintentos, cancelación, trazabilidad y escalado de workers.

### ¿Por qué existe `processor_adapter`?

Para desacoplar la plataforma del procesador externo. La API y el worker no dependen de detalles clínicos o scripts concretos.

### ¿Por qué filesystem local y no S3/MinIO?

Para el alcance del TFM reduce complejidad operativa. El diseño documenta esa limitación y deja S3/MinIO como evolución futura.

### ¿Qué pasa con datos sensibles?

La versión asume estudios anonimizados. No se deben usar datos identificativos. El sistema añade permisos, trazabilidad y retención operativa, pero no sustituye una política institucional completa de seguridad.

### ¿Por qué la retención no está en GUI?

Porque borrar datos es una operación sensible. Se dejó por CLI/configuración, con dry-run y borrado real desactivado por defecto, para evitar ejecuciones accidentales desde una pantalla web.

### ¿Cómo se integraría otro pipeline?

Cambiando o extendiendo la configuración y el adaptador, manteniendo la frontera en `processor_adapter` y sin acoplar FastAPI al algoritmo.

### ¿Qué aporta frente a ejecutar el script manualmente?

Propiedad de estudios, cola asíncrona, auditoría, logs, artefactos descargables, permisos, notificaciones, dashboard, backup, retención y documentación operativa.

## Checklist Rápida Antes De Empezar

- Servicios levantados con `make up`.
- Migraciones aplicadas con `make migrate`.
- `make smoke` correcto.
- Admin y researcher creados.
- Estudio completado preparado.
- Fichero `.nii.gz` anonimizado listo.
- Mailpit abierto si se enseña correo.
- Swagger abierto.
- No tener que ejecutar `make clean`, `restore` ni procesamiento largo en directo.

## Cierre Recomendado

Frase final:

> La plataforma demuestra una base transferible para integrar procesamiento de neuroimagen en un entorno web trazable: separa responsabilidades, evita bloquear la API, conserva auditoría, protege acceso por roles y documenta claramente qué está implementado y qué queda para una fase posterior.
