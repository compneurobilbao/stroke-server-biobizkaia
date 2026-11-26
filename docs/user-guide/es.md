# Guía de usuario para investigadores del laboratorio

English version: [en.md](en.md).

Esta guía explica cómo ejecutar el pipeline de análisis mínimo desde la plataforma web. Está escrita para investigadores, no para desarrolladores. Los nombres en **negrita** son las etiquetas que aparecen en pantalla (interfaz en español).

La plataforma es **solo para uso en investigación**. Sus resultados no son aptos para decisiones clínicas, y cada página del PDF lo indica.

## 1. Qué hace la plataforma hoy

Abra **Sobre el proceso** en la navegación principal para ver el esquema general ("Cómo una lesión se convierte en un informe de investigación"). Hoy la plataforma parte de una máscara binaria de lesión que ya está en espacio estándar (MNI152NLin6Asym, 2 mm) y ejecuta dos análisis sobre ella:

- **Desconexión funcional**: el mapeo funcional de redes lesionales (flnm-fast) frente a un conectoma funcional normativo da un mapa de desconexión; a partir de ese mapa se estima el perfil cognitivo COGNET.
- **Desconexión estructural**: la desconexión por tracto (lnm-tracts) con los tractos de sustancia blanca HCP1065 da una tabla de tractos.

Ambos resultados se reúnen en un **Informe de investigación** (PDF) y un visor interactivo.

El esquema muestra además etapas en gris marcadas como **En desarrollo**: **Normalización a MNI**, **Segmentación de la lesión**, **Banco de lesiones** y **LNM estructural vóxel a vóxel**. Todavía no están disponibles y no se pueden seleccionar.

## 2. Iniciar sesión

1. Abra la dirección de la plataforma que le haya dado el administrador.
2. Escriba su **Correo electrónico** y su **Contraseña** y pulse **Entrar**.
3. Use **Cerrar sesión** al terminar. Si aparece "La sesión ha expirado. Vuelve a iniciar sesión.", inicie sesión de nuevo.

Las cuentas las crea un administrador; no hay registro público. El idioma se cambia con el selector **Idioma** (English o Español).

## 3. Preparar la máscara de lesión

Necesita un único archivo: una máscara de lesión en formato `.nii.gz` ya registrada en MNI152NLin6Asym. Hoy la plataforma no normaliza máscaras por usted.

La subida se comprueba en dos fases.

**Al subir** (el archivo se rechaza de inmediato):

- El nombre del archivo debe terminar en `.nii.gz`, el archivo no puede estar vacío y debe ser una imagen NIfTI comprimida con gzip.
- No puede superar el tamaño máximo de subida del despliegue (1024 MB salvo que el administrador lo haya cambiado).

**Paso de control de calidad de la lesión** (el primer paso de cada ejecución; la ejecución se detiene si la máscara no lo supera):

- Binaria: solo los valores 0 y 1.
- Un único volumen 3D (se acepta un único volumen final) con una matriz vóxel a mundo válida.
- No vacía, y no vacía tras remuestrear a la rejilla de trabajo de 2 mm (las lesiones muy pequeñas pueden desaparecer).
- La cabecera debe declarar un sistema de coordenadas, no debe declarar espacio nativo, los ejes no deben ser oblicuos y la lesión debe estar dentro de la extensión de MNI152.
- Volumen no superior al máximo (800 mL por defecto).

Después el paso remuestrea la máscara a la rejilla de 2 mm y registra su volumen, hemisferio, componentes conexas y caja envolvente. También registra avisos que no detienen la ejecución, por ejemplo una lesión pequeña (menos de 0,5 mL por defecto), varias componentes, una lesión en ambos hemisferios o un volumen que cambia tras el remuestreo.

No ponga nombres ni identificadores en los archivos ni en los datos del caso. Los nombres de archivo no se guardan.

## 4. Crear un caso

Un caso contiene su archivo de entrada y sus ejecuciones. Vaya a **Casos** y pulse **Nuevo caso**. El asistente tiene cuatro pasos (**Progreso del asistente**): **Modo y datos del caso**, **Archivos**, **Implementaciones** y **Análisis**.

1. **Modo y datos del caso**. En **Modo de entrada** elija **Máscara de lesión ya en MNI**. El otro modo, **TC y máscara de lesión**, necesita la etapa de normalización, que está en desarrollo, así que la instancia del laboratorio no lo ejecuta. Opcionalmente indique **Sexo (opcional)** y **Banda de edad (opcional)**. Solo se guarda la banda; la edad exacta nunca se envía al servidor. La plataforma guarda solo una etiqueta generada para el caso.
2. **Archivos**. Seleccione su archivo `.nii.gz` ("Máscara de lesión en espacio MNI (.nii.gz)"). Opcionalmente declare **¿Cómo se obtuvo esta máscara? (opcional)**: **Dibujada a mano** o **Creada con una herramienta externa** (indique **Nombre de la herramienta** y **Versión de la herramienta**; solo letras, dígitos y `. _ + -`, hasta 64 caracteres). Se muestra en la ejecución y en el informe como "Declarado por el usuario (no verificado)".
3. **Implementaciones**. Cada paso usa la implementación predeterminada salvo que elija otra. Vea la sección 6 para la cohorte.
4. **Análisis**. Elija qué ejecutar (sección 5).

Por último pulse **Crear caso e iniciar ejecución**.

### Probar la plataforma con el ejemplo sintético

Si su despliegue lo ofrece, el paso **Archivos** muestra **Usar la lesión de ejemplo (sintética)**. Selecciónela en lugar de subir un archivo. Es una máscara sintética, no son datos de pacientes, y la ejecución y el informe la marcan como ejemplo sintético. Pulse **Quitar** para volver a subir su propio archivo. Un caso nunca mezcla un ejemplo con archivos propios.

Más adelante puede iniciar otra ejecución sobre el mismo caso con **Ejecutar de nuevo con el mismo plan** o **Ejecutar de nuevo con archivos nuevos**. El origen declarado de un caso no se puede cambiar después de su primera ejecución ("El origen declarado no se puede cambiar después de la primera ejecución.").

## 5. Elegir un análisis (preset o plan de ejecución)

En el paso **Análisis** la plataforma lista los análisis que ofrece. El preseleccionado lleva la etiqueta **Recomendado**. Un análisis marcado como **No disponible** no puede ejecutarse ahora en este despliegue.

| Opción | Significado |
|---|---|
| Un análisis con nombre (preset) | Un conjunto de resultados revisado y versionado. La instancia del laboratorio ofrece **Disconnection analysis of an MNI lesion mask** (el título del preset se muestra en inglés): control de calidad de la lesión, desconexión funcional, perfil COGNET, desconexión por tractos e informe de investigación. Todos estos resultados son obligatorios: si alguno no puede producirse, la ejecución se rechaza en lugar de ejecutarse en silencio sin él. |
| **Plan completo** | Todos los pasos que se aplican al modo de entrada. |
| **Personalizado** | Usted elige los resultados; los pasos que necesitan se añaden automáticamente. Cada resultado es **Obligatorio** (la ejecución se rechaza si no puede producirse) o **Si está disponible** (se produce cuando es posible y se omite en caso contrario). |

Existen otros presets (**Full lesion network analysis**, **Lesion QC and report**, **Normalisation of a CT lesion to MNI152**), pero un despliegue ofrece solo los que su administrador haya habilitado.

**Quién puede usar Personalizado.** Lo decide el ajuste del despliegue `RUN_PLAN_POLICY`:

| Perfil de despliegue | Personalizado (modo libre) |
|---|---|
| `research-public`, `mask-only` | Nadie; solo presets. |
| `on-premise` (instancia del laboratorio) | Administradores e investigadores por defecto (`free_for_researchers`); el administrador puede restringirlo a `free_for_admins` o `presets_only`. |

Si Personalizado no está permitido para usted, el panel del plan dice "Elegir sus propios resultados no está permitido para su cuenta en este despliegue. Elija uno de los análisis."

Bajo las opciones, el panel **Plan** muestra qué pasos se ejecutarán y por qué: **Solicitado**, **Añadido por el plan**, **Si está disponible**, **No forma parte de este plan**. También dice "Este plan puede ejecutarse en este despliegue." o "Este plan no puede ejecutarse. Vea los motivos más abajo." con los motivos.

## 6. Elegir la cohorte normativa

El paso de desconexión funcional compara su lesión con una cohorte normativa sana. En el paso **Implementaciones**, el paso de mapeo funcional de redes lesionales ofrece:

- **flnm-fast · HCP Envejecimiento** (predeterminada)
- **flnm-fast · HCP Adultos jóvenes**

Elija la cohorte que mejor se ajuste a la población que estudia. El paso usa todos los participantes de la cohorte elegida: no hay emparejamiento por edad ni sexo, de modo que el sexo y la banda de edad del caso no afectan al resultado.

Algunos análisis fijan una implementación (**Fijada por este análisis**); esas no se pueden cambiar. El paso del informe puede ofrecer una implementación en inglés y otra en español con el mismo título; elija según el idioma que quiera para el PDF.

Junto a cada implementación la plataforma muestra su nivel de validación y, en las no validadas, la etiqueta **experimental** (vea la sección 10).

## 7. Qué produce cada análisis

### Control de calidad de la lesión

Una figura de control de calidad y un registro (volumen, hemisferio, componentes, caja envolvente, rejilla, avisos), además de la máscara de lesión en la rejilla de trabajo de 2 mm.

### Mapa de desconexión funcional (flnm-fast)

Un mapa con una puntuación z con signo para cada parcela de la parcelación normativa, escrito en la rejilla de 2 mm. Un valor positivo indica que la parcela está acoplada positivamente con el sitio de la lesión. El mapa es constante dentro de cada parcela y cero fuera de la parcelación. Una parcela cuyo estadístico no está definido no tiene valor (NaN). El paso rechaza una lesión que no se solapa con ninguna parcela (`lesion_outside_parcellation`).

### Tabla de tractos (lnm-tracts)

Una fila por tracto del atlas HCP1065 con la **desconexión** (la fracción de las líneas de flujo del tracto que atraviesan la lesión, de 0 a 1) y los recuentos de líneas. El atlas de tractos está definido en el espacio ICBM 2009a no lineal asimétrico, no en el espacio de su máscara. Por eso el paso remuestrea la lesión al espacio del atlas por vecino más próximo antes de comprobar las líneas de flujo. Los recuentos de vóxeles y los volúmenes antes y después quedan registrados en la procedencia (aviso `lesion_resampled_to_atlas_space`). Revíselos si la lesión es pequeña, porque el remuestreo puede cambiar su volumen.

### Perfil cognitivo COGNET

Una decodificación metaanalítica del mapa de desconexión. Para cada concepto da una puntuación y un valor p de permutación corregido por Bonferroni, para tres medidas: desconexión positiva, desconexión negativa y solapamiento con la lesión. Los conceptos se agrupan en dominios. Las parcelas sin valor en el mapa de desconexión se excluyen de las puntuaciones de desconexión y se señalan en el perfil y en el informe (número y fracción de parcelas excluidas). El solapamiento con la lesión no usa ese mapa y no se ve afectado. El paso rechaza un mapa sin ningún valor definido en ninguna parcela (`disconnection_map_undefined`).

### Informe de investigación (PDF)

Cada página lleva el aviso de uso solo en investigación. Secciones:

1. **Resumen**: qué análisis están disponibles, no solicitados o fuera del análisis; el plan de análisis (plan completo, preset con su versión o resultados personalizados) y, cuando corresponde, la línea "Entrada de ejemplo: lesión sintética, no son datos de pacientes".
2. **Lesión**: vistas ortogonales por el centroide de la lesión, volumen, hemisferio, componentes, centroide, rejilla y los avisos registrados por el control de calidad. El fondo de la figura es una plantilla MNI152 usada solo como orientación, así que son esperables pequeños desplazamientos respecto a la rejilla de su máscara.
3. **Desconexión**: 3.1 el mapa funcional (máximo, número de vóxeles mayores que cero, media de los vóxeles positivos, vóxeles sin valor) y 3.2 la tabla de tractos (desconexión por tracto; la figura muestra los tractos más desconectados, hasta 20).
4. **Perfil cognitivo**: método, puntuaciones y valores p corregidos por dominio y medida, y los conceptos significativos tras la corrección. Si no hay ninguno, lo indica. También indica cuántas parcelas se dejaron fuera.
5. **Procedencia**: versión y huella del pipeline, pasos con las huellas de sus imágenes de contenedor, paquetes de datos de referencia, artefactos leídos, herramientas externas y el origen de las entradas declarado por el usuario.
6. **Notas**: notas fijas, por ejemplo que se excluyeron parcelas del perfil cognitivo, que una entrada es un ejemplo sintético o que un origen fue declarado por el usuario y no está verificado.

Una sección no solicitada indica "No solicitado" y queda vacía.

## 8. Seguir una ejecución y descargar los resultados

Al pulsar **Crear caso e iniciar ejecución** se abre la página de **Ejecución**. Muestra **Pipeline**, **Conjunto de entradas**, **Creada**, **Finalizada** y la **Cronología de pasos**. Cada paso muestra su estado (**En cola**, **En ejecución**, **Completado**, **Fallido**, **Cancelado**, **Omitido**, **No disponible**), su implementación y quién la eligió, cada **Intento** y un registro (**Ver registro**). Los pasos fuera del plan figuran como "No forma parte de este plan". Las etapas **En desarrollo** figuran como **No forma parte de esta ejecución**.

Para obtener los resultados:

- **Informes**: **Descargar informe PDF**.
- **Artefactos**: una tabla con **Salida**, **Tipo** y **Tamaño** y un botón **Descargar** por archivo (el mapa funcional, la tabla de tractos, el perfil COGNET, los archivos de control de calidad, etc.). Los artefactos que proceden del ejemplo sintético llevan la etiqueta **sintético**.
- **Ver imágenes** abre un visor de solo lectura con la máscara de lesión y el mapa de desconexión como capas (**Mostrar**, **Opacidad**, **Umbral**). Si la ejecución no tiene una TC en espacio MNI, no se muestra fondo anatómico.
- **Abrir caso** vuelve al caso.

Puede detener una ejecución con **Cancelar ejecución**; se detiene tras el paso actual.

## 9. Cuando una ejecución falla

Si la ejecución no puede iniciarse, el asistente muestra "El servidor ha rechazado iniciar esta ejecución:" seguido de los motivos. Los más habituales:

| Mensaje | Qué hacer |
|---|---|
| "... no tiene ninguna implementación habilitada en este despliegue..." | Pida al administrador que habilite el paso, o elija un análisis que no lo necesite. |
| "El análisis ... no está habilitado en este despliegue." | Elija otro análisis. |
| "El análisis ... no acepta los archivos de este modo de entrada." | Use el otro modo de entrada u otro análisis. |
| "Elegir sus propios resultados no está permitido..." | Elija un análisis con nombre. |
| "No está disponible un paquete de datos de referencia que necesita ..." | Los datos de la cohorte, del atlas o de los conceptos no están instalados; consulte al administrador. |

Si una ejecución falla mientras corre, abra el paso fallido en la página de **Ejecución**. El campo **Fallo** y el paso muestran un mensaje breve, y **Ver registro** da más detalle. La pantalla muestra el mensaje y no el código, así que relacione el mensaje con la tabla siguiente (los códigos también aparecen en los registros y en la API). Si la causa está en su archivo, corríjalo y cree un caso nuevo, porque los archivos de un caso no se pueden cambiar tras su primera ejecución. Si la causa fue temporal, use **Reintentar desde el paso fallido** (o **Reanudar desde el paso cancelado** tras una cancelación); los pasos que ya terminaron bien no se repiten.

| Código | Paso | Significado |
|---|---|---|
| `input_missing` | todos | No se proporcionó una entrada obligatoria. |
| `input_format_invalid` | todos | El archivo no es una imagen NIfTI comprimida con gzip, o la máscara tiene valores distintos de 0 y 1. |
| `invalid_parameters` | todos | Los parámetros no cumplen el esquema del paso. |
| `lesion_input_missing` | control de calidad | No se proporcionó ninguna máscara de lesión. |
| `conflicting_inputs` | control de calidad | Se proporcionó una máscara MNI junto con una TC o una máscara nativa. |
| `mask_not_3d` | control de calidad | La máscara tiene más de tres dimensiones. |
| `affine_invalid` | control de calidad | La matriz vóxel a mundo es singular o no es finita. |
| `mask_empty` | control de calidad, flnm-fast, lnm-tracts, COGNET | La máscara no tiene ningún vóxel activo. |
| `mask_empty_after_resampling` | control de calidad | La lesión es demasiado pequeña para sobrevivir al remuestreo a la rejilla de 2 mm. |
| `space_not_declared` | control de calidad | La cabecera no declara sistema de coordenadas, así que no se puede confirmar el espacio MNI152. |
| `space_mismatch` | control de calidad | La cabecera declara espacio nativo, los ejes son oblicuos o la lesión queda fuera de la extensión de MNI152. |
| `ct_grid_mismatch` | control de calidad | La TC y la máscara nativa no están en la misma rejilla. |
| `lesion_too_large` | control de calidad | El volumen supera el máximo. |
| `bilateral_lesion` | control de calidad | La lesión afecta a ambos hemisferios y la ejecución está configurada para rechazarlas (si no, es solo un aviso). |
| `lesion_outside_parcellation` | flnm-fast, COGNET | La lesión no se solapa con ninguna parcela de la parcelación. |
| `mask_outside_atlas_space` | lnm-tracts | No queda ningún vóxel de la lesión tras remuestrear al espacio del atlas. |
| `disconnection_not_finite` | COGNET | El mapa de desconexión tiene valores infinitos. |
| `disconnection_map_undefined` | COGNET | El mapa no tiene ningún valor definido en ninguna parcela. |
| `disconnection_map_empty` | COGNET | El mapa es cero allí donde está definido. |

Si el mensaje no explica el problema, envíe al administrador el enlace de la página de la ejecución y el registro del paso.

## 10. Hasta dónde fiarse de los resultados

Todos los pasos del pipeline están **probados con referencia** (reference-tested): su salida se comparó en pruebas automáticas con un cálculo independiente sobre datos sintéticos o públicos. **Ningún paso está validado**, porque todavía no existe un registro de validación firmado. Por tanto:

- Use los resultados solo para investigación, nunca para decisiones clínicas.
- Los perfiles de despliegue públicos habilitan solo pasos validados, así que todavía no ofrecen ningún análisis; la instancia del laboratorio ejecuta pasos probados con referencia y los marca como **experimental**.
- La tabla de tractos y el perfil COGNET dependen de datos de referencia (atlas, mapas de conceptos, cohorte) fijados por versión; la sección de procedencia del informe los lista. Cite las versiones del pipeline y de los paquetes cuando describa un resultado.
- La plataforma no puede verificar el origen declarado de una máscara.
