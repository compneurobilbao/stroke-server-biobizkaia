# Roadmap

Este roadmap ordena la evolución funcional de la plataforma desde el estado actual del TFM hacia una base realista para uso investigador u hospitalario futuro. No implica que las fases estén implementadas: documenta prioridad, dependencias, riesgos y criterios de aceptación antes de construir nuevas capacidades.

## Estado Del Proyecto

La plataforma actual permite login local, roles básicos `admin`/`researcher`, propietario por estudio, subida de un T1w `.nii.gz`, preparación BIDS, encolado con Celery/Redis, worker `compneuro`, renderizado NIfTI a PNG con FSL `slicer`, artefactos técnicos, descarga de resultados según permisos, gestión básica de jobs, dashboard operativo para admin, backup/restore local por CLI, compartición temporal de PDF técnicos, notificaciones internas/correo electrónico y cuotas básicas por usuario.

Todavía no existen flujos seleccionables desde GUI, pantalla admin avanzada de retención ni revisión clínica formal con revisor, fecha y comentarios.

## Roadmap de evolución funcional

### Fase 0 — Estado Actual Consolidado

Funcionalidades disponibles:

- Subida manual de T1w `.nii.gz`.
- Preparación BIDS para un sujeto por estudio.
- Procesamiento asíncrono con Celery/Redis.
- Worker basado en `compneuro-anatproc`.
- Renderizado PNG desde NIfTI.
- PDF técnico y ZIP de resultados.
- Descarga de resultados desde la GUI.
- Auditoría básica asociada al estudio.

Criterios de aceptación ya cubiertos:

- La API no ejecuta procesamiento largo dentro del ciclo HTTP.
- `processor_adapter` sigue siendo la frontera con el procesador externo.
- Los estudios quedan trazados en PostgreSQL y filesystem local.
- `make test`, `make lint`, `make check-docs` y `make check-secrets` son los controles principales del proyecto.

### Fase 1 — Multiusuario Básico

Estado: implementada como base funcional.

Objetivo: introducir identidad, propiedad de estudios y permisos mínimos sin cambiar el flujo de procesamiento.

Alcance implementado:

- Login local.
- Usuarios creados por admin; sin registro público abierto.
- Roles iniciales `admin` y `researcher`.
- Sesión segura con expiración.
- Protección de endpoints sensibles.
- `owner_user_id` en `Study`.
- Historial de estudios por usuario.
- Migración de estudios existentes a usuario `system` o `admin`.
- Auditoría básica de login, upload, download y delete.

Dependencias:

- Modelo `User` y migración Alembic.
- Hash robusto de contraseñas.
- Decisión documentada de cookie session o JWT simple.
- Definición de permisos por endpoint antes de tocar la GUI.

Riesgos:

- Fuga de estudios si algún endpoint no filtra por propietario.
- Migración incompleta de estudios previos.
- Sesiones mal configuradas en navegador si se elige JWT sin política clara de expiración y almacenamiento.
- Mezclar auth con lógica de procesamiento en lugar de mantenerla en la capa API.

Criterios de aceptación:

- Existe login local funcional.
- Existe al menos un usuario admin inicial.
- Los estudios tienen propietario.
- `researcher` solo ve sus propios estudios.
- `admin` ve todos los estudios.
- Los endpoints sensibles están protegidos.
- La GUI muestra historial por usuario.
- La documentación explica creación de usuarios, roles y permisos.
- Tests básicos de permisos pasan.
- Se mantiene compatibilidad técnica con el flujo `compneuro`.

### Fase 2 — Gestión De Jobs Y Trazabilidad

Estado: implementada como base funcional.

Objetivo: mejorar control operativo de jobs sin introducir todavía administración avanzada.

Alcance implementado:

- Estados de job más detallados, sin porcentajes falsos.
- Logs visibles en GUI con truncado.
- Logs finales descargables o visibles de forma segura.
- Cancelación de jobs en cola y solicitud de cancelación de procesamientos en ejecución.
- Retry de jobs fallidos.
- Soft delete en base de datos.
- Borrado físico controlado de input, BIDS, output, PNG, PDF, ZIP y logs.
- Auditoría ampliada.
- Vista detalle de job.

Riesgos:

- Exponer rutas internas sensibles en logs.
- Romper trazabilidad si el borrado físico elimina toda evidencia operativa.
- Reintentos no idempotentes si no se limpian resultados parciales.

Criterios de aceptación:

- El usuario propietario y el admin pueden ver detalle de job según permisos.
- Los logs se muestran truncados y sin rutas internas innecesarias.
- Los jobs en cola pueden cancelarse antes de entrar en ejecución y los jobs en procesamiento pueden recibir una solicitud de terminación trazable.
- El borrado conserva auditoría mínima.
- El retry de un job fallido deja trazabilidad del intento anterior.

### Fase 3 — Admin Dashboard

Objetivo: dar al administrador visibilidad operativa mínima.

Estado implementado: dashboard básico accesible solo para `admin` mediante la GUI y `GET /api/admin/dashboard`.

Alcance implementado:

- Estado de cola.
- Jobs activos y fallidos.
- Uso de disco.
- Healthchecks.
- Estado worker, Redis y PostgreSQL.
- Usuarios.
- Estudios por estado.
- Alertas básicas.

Dependencias:

- Fase 1 para rol admin.
- Fase 2 para estados y trazabilidad más útiles.

Criterios de aceptación:

- El dashboard solo es accesible para admin.
- Muestra datos globales sin consultar manualmente la base de datos.
- Las alertas no bloquean el procesamiento.

### Fase 4 — Backups Y Mantenimiento

Objetivo: documentar y automatizar una recuperación local razonable para TFM sin infraestructura externa compleja.

Estado: implementada como operación CLI mediante `make backup` y `make restore`, sin endpoints ni GUI.

Alcance implementado:

- Script local de backup PostgreSQL.
- Script local de backup de `data/studies`.
- Script de restore documentado.
- Smoke test tras restore.
- Comandos Makefile.
- Documentación de operación.
- Política manual de mantenimiento.

Decisiones cubiertas:

- La unidad restaurable es base de datos + `data/studies`.
- Los backups se guardan por defecto en `backups/`, fuera de Git, y deben copiarse fuera del servidor para protección real ante pérdida de disco.

Criterios de aceptación:

- Un operador puede hacer backup y restore siguiendo la documentación.
- Tras restore, `make smoke` o check equivalente confirma que la API responde.
- La documentación advierte que DB y filesystem deben restaurarse juntos.

### Fase 5 — Compartición Segura De Informes

Objetivo: compartir resultados sin crear cuentas completas para receptores externos.

Estado: implementada para descarga de PDF técnico mediante token temporal opaco, con caducidad, revocación y auditoría. No comparte ZIP ni logs.

Alcance recomendado:

- Enlaces temporales opacos.
- Caducidad configurable.
- Revocación.
- Auditoría de acceso.
- Descarga PDF mediante token.
- Descarga ZIP opcional si la política lo permite en una fase posterior.
- Viewer externo sin cuenta completa o rol `viewer` futuro queda fuera.

Dependencias:

- Fase 1 para propietario y permisos.
- Fase 2 para auditoría ampliada.

Riesgos:

- Tokens demasiado largos de vida.
- Enlaces no revocables.
- Compartición de ZIP con más datos de los necesarios.

Criterios de aceptación:

- El propietario o admin puede crear y revocar enlaces.
- Los enlaces caducan.
- Cada descarga queda auditada.
- El endpoint por token no permite enumerar estudios.

### Fase 6 — Notificaciones

Objetivo: avisar de finalización o fallo sin adjuntar datos pesados.

Estado: implementada con notificaciones internas, preferencias por usuario y SMTP configurable; Docker Compose usa Mailpit local por defecto.

Alcance implementado:

- SMTP configurable.
- Correo electrónico al completar o fallar.
- Solo enlaces; sin adjuntar PDF, ZIP ni datos pesados.
- Preferencias de notificación.
- Notificación a admin en errores críticos.
- Notificaciones internas en UI.

Dependencias:

- Fase 1 para correo electrónico institucional de usuario.
- Fase 5 si los correos enlazan a recursos compartidos temporales.

Criterios de aceptación:

- El sistema no envía adjuntos pesados.
- Los errores de SMTP no marcan como fallido el procesamiento principal.
- Las notificaciones quedan registradas para diagnóstico.

### Fase 7 — Subida Múltiple Y Lotes

Objetivo: permitir cargar varios estudios sin cambiar la interfaz de procesamiento esperada por fichero.

Alcance recomendado:

- Subida múltiple de `.nii.gz`.
- Un job por fichero.
- Entidad `BatchUpload` opcional para agrupar el lote.
- Tabla filename -> subject ID.
- Resultado parcial si algunos fallan.
- Descarga agregada opcional.

Decisión recomendada:

- Mantener un job por fichero. El lote agrupa trazabilidad y experiencia de usuario, no fusiona procesamiento.

Riesgos:

- Que un fallo parcial bloquee todo el lote.
- Saturar disco o cola sin límites.
- Ambigüedad entre nombre de fichero y sujeto BIDS.

Criterios de aceptación:

- Cada fichero produce un estudio/job independiente.
- El usuario puede ver éxito/fallo por fichero.
- El lote no oculta errores individuales.

### Fase 8 — Retención, Cuotas Y Control De Almacenamiento

Objetivo: controlar crecimiento de datos y costes operativos.

Estado: cuota básica por usuario implementada; retención inicial por CLI/configuración implementada con `keep_forever`, dry-run, auditoría y borrado físico, pero desactivada por defecto para ejecución real. Quedan pendientes una pantalla admin avanzada y limpieza manual guiada.

Alcance recomendado:

- Política de retención por CLI/configuración.
- Recomendación inicial: 90 días.
- Flag `keep_forever`.
- Dry-run por defecto.
- Auditoría de borrado automático.
- Evolución de cuota por usuario ya implementada como límite básico de subida.
- Alerta de uso de disco.
- Limpieza manual controlada.

Dependencias:

- Fase 1 para cuotas por usuario.
- Fase 2 para borrado seguro y auditoría.
- Fase 3 para alertas de disco.

Criterios de aceptación:

- El dry-run muestra qué se borraría sin eliminar datos.
- `keep_forever` excluye estudios de retención automática.
- Todo borrado automático genera evento de auditoría.

### Fase 9 — Flujos Configurables

Objetivo: preparar la selección de flujos de procesamiento sin convertir la plataforma en un gestor dinámico complejo de herramientas.

Alcance recomendado:

- Selección de flujo de procesamiento por subida.
- Registro de `pipeline_name` y `pipeline_version`.
- Habilitar/deshabilitar flujos por configuración.
- Mantener `compneuro` como flujo principal.
- Mantener `dummy` como flujo de desarrollo.
- Gestión avanzada de herramientas solo como futuro lejano.

Decisión recomendada:

- Preparar modelo/configuración antes de UI avanzada.
- Seguir usando `processor_adapter` como frontera obligatoria.

Riesgos:

- Acoplar API o worker a detalles internos de cada flujo.
- Permitir combinaciones de entradas y flujos no validadas.

Criterios de aceptación:

- Cada estudio registra el flujo usado y su versión.
- La selección no rompe el flujo compneuro actual.
- Los flujos deshabilitados no aparecen en la GUI.

### Fase 10 — Integración Institucional

Objetivo: preparar el sistema para usuarios institucionales e investigadores con seguridad reforzada.

Alcance recomendado:

- Google login/OIDC.
- Restricción por dominio institucional.
- ORCID login para investigadores.
- DICOM anonymization check.
- Preparación para datos reales.
- Hardening de despliegue.
- TLS real.
- Auditoría reforzada.

Decisión recomendada:

- Login local primero, OIDC después. Ambos deben poder convivir.
- Google/OIDC es deseable para entorno institucional.
- ORCID solo gana prioridad si el uso investigador pesa más que el hospitalario.

Riesgos:

- Complejidad de identidad externa antes de tener permisos internos sólidos.
- Confundir check de anonimización con anonimización garantizada.

Criterios de aceptación:

- Los proveedores externos se vinculan a usuarios internos.
- La restricción de dominio se configura fuera del código.
- La auditoría diferencia login local y login externo.

### Fase 11 — Revisión Clínica

Objetivo: separar resultados técnicos de una eventual validación profesional.

Alcance recomendado:

- Flag `technical_only`.
- Estado `reviewed`.
- Estado `validated`.
- Usuario revisor.
- Fecha de revisión.
- Comentarios.

Dependencias:

- Fase 1 para identidad de revisores.
- Fase 2 para trazabilidad.
- Definición clínica/legal externa al desarrollo software.

Criterios de aceptación:

- Solo usuarios autorizados pueden marcar revisión o validación.
- La revisión no modifica resultados originales.
- El historial de cambios queda auditado.

## Priorización Recomendada

La próxima fase óptima es **Fase 7 — Subida Múltiple Y Lotes**.

Justificación:

- La Fase 1 ya establece identidad, roles, propietario por estudio e historial básico.
- La Fase 2 ya añade detalle de trabajo, logs truncados, cancelación de trabajos en cola o en ejecución, reintento y borrado seguro.
- La Fase 3 ya aporta visibilidad operativa global para admin.
- Backup y restore local ya están cubiertos como operación CLI antes de avanzar a lotes, retención o integración institucional.
- La Fase 5 ya permite compartir PDF técnicos con enlaces temporales, revocables y auditados.

Orden óptimo de implementación tras cerrar este roadmap:

1. Fase 7: subida múltiple y lotes.
2. Fase 8: retención, cuotas y almacenamiento.
3. Fase 9: flujos configurables.
4. Fase 10: integración institucional.
5. Fase 11: revisión clínica.

## Histórico Implementado

Las fases 1, 2, 3, 4, 5 y 6 ya están implementadas como base funcional y operativa:

- Fase 1: login local, roles `admin`/`researcher`, propietario por estudio, creación de usuarios por admin y permisos por endpoint.
- Fase 2: detalle de trabajos, logs truncados, cancelación de trabajos en cola o en ejecución, reintento de fallidos, soft delete, borrado físico controlado y auditoría mínima.
- Fase 3: dashboard admin con cola, trabajos activos/fallidos, uso de disco, healthchecks, usuarios, estudios por estado y alertas no bloqueantes.
- Fase 4: backup/restore local por CLI de PostgreSQL y `data/studies`, con confirmación fuerte para restore y smoke test posterior.
- Fase 5: enlaces temporales y revocables para descargar PDF técnicos sin cuenta completa, con hash de token y auditoría de accesos.
- Fase 6: notificaciones internas, preferencias por usuario y correo electrónico SMTP configurable.

Los detalles técnicos vivos están en `docs/architecture.md`, `docs/api.md`, `docs/developer-manual.md` y los tests.

## No Implementar Todavía

- Google login.
- ORCID login.
- Rol `viewer` completo.
- Compartición de ZIP por enlace.
- Correo electrónico institucional avanzado.
- Retención automática.
- Subida múltiple.
- Pipeline manager avanzado.
- 2FA.
- Retención avanzada con `keep_forever`, dry-run y limpieza automática.
- Anonimización DICOM real.

## Decisiones Pendientes

- Evaluar si conviene migrar de JWT en localStorage a cookie `HttpOnly` antes de exposición real.
- Definir si `AuditEvent.actor` textual se conserva como compatibilidad o se elimina en una migración futura.
- Decidir si el usuario `system` debe ocultarse del dashboard admin.
- Definir política de retención antes de usar datos sensibles.
