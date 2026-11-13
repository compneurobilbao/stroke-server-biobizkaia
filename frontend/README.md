# Frontend

Esta carpeta contiene la GUI web sencilla en castellano. Está implementada con React y Vite, y se sirve como sitio estático detrás del reverse proxy.

## Responsabilidad

- Mostrar login local y mantener sesión JWT en cliente.
- Mostrar una pantalla clara para subir estudios anonimizados.
- Listar estudios visibles según permisos.
- Mostrar estados de procesamiento.
- Permitir descargar PDF/ZIP cuando estén disponibles y el usuario tenga permiso.
- Mostrar detalle de jobs y logs truncados.
- Permitir a researchers cancelar jobs en cola o en procesamiento y reintentar fallidos; permitir borrar estudios permitidos que no están en ejecución.
- Permitir gestión básica de usuarios, estado activo y cuotas para admin.
- Mostrar dashboard operativo para admin.
- Mostrar notificaciones internas y preferencias de correo.
- Permitir compartir PDF técnicos mediante enlaces temporales y revocables.
- Permitir marcar resultados como `Solo técnico`, `Revisado` o `Validado` para trazabilidad interna.
- Mostrar advertencias de uso responsable del PDF técnico.

## Estructura Del Código

- `src/main.tsx`: entry point (router, i18n, styles).
- `src/App.tsx`: providers and routes (`/login`, `/studies`, `/notifications`, `/admin`; `/` redirects to `/studies`).
- `src/api/`: shared API client (`client.ts`: bearer token, 401 handling, `ApiError` with `detail` and optional `code`) and payload types.
- `src/features/auth|studies|notifications|admin/`: one module per feature (pages, components, API calls).
- `src/layout/`: page shell, navigation and language switch.
- `src/shared/`: formatting helpers, message context, status badge.
- `src/i18n/`: `react-i18next` setup; catalogues in `locales/en.json` (source) and `locales/es.json`. The language switch is persisted in `localStorage`.
- `src/styles.css`: define el estilo visual de la interfaz.
- `index.html`: punto de entrada HTML de Vite.
- `package.json`: scripts y dependencias frontend.
- `package-lock.json`: versiones resueltas de dependencias frontend para instalaciones reproducibles.
- `eslint.config.js`: configuración de lint compatible con ESLint 9.
- `Dockerfile`: compila el frontend y lo sirve con Nginx.
- `nginx.conf`: configuración mínima para servir la SPA.

## Desarrollo Y Validación

```bash
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

All dependencies are pinned to exact versions and `package-lock.json` is committed; use `npm ci`. New UI strings go into both catalogues (a test checks that the key sets match).

## Flujo En La UI

1. El usuario inicia sesión contra `/api/auth/login`.
2. La UI guarda el token local y consulta `/api/auth/me`.
3. El usuario selecciona un fichero.
4. La UI envía `POST /api/studies/upload` con `FormData` y `Authorization: Bearer`.
5. La UI refresca el listado de estudios permitido para el usuario.
6. Cada 5 segundos consulta `/api/studies` para actualizar estados.
7. Si el usuario es `admin`, consulta `/api/admin/dashboard` y lo refresca periódicamente.
8. La tabla permite ver detalle/logs y ejecutar acciones según estado del estudio.
9. Si `has_pdf` o `has_output_zip` es verdadero, descarga con `fetch` autenticado.
10. Si el estudio completado tiene PDF, permite crear/revocar enlaces temporales de descarga.
11. La UI muestra notificaciones internas y permite activar/desactivar correos de cierre.

## Criterio De Diseño

La interfaz es deliberadamente simple. Incluye login local, historial por usuario, gestión básica de jobs, dashboard operativo, usuarios/cuotas admin, sharing de PDF, notificaciones y una marca de revisión técnica. No incluye revisión clínica formal, lotes, pantalla GUI de retención ni flujos hospitalarios futuros.
