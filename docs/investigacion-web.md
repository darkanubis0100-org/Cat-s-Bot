# Investigación web y Deep Think

La investigación web es una función opcional de Strata. La búsqueda se limita a información pública y trata las páginas recuperadas como contenido no confiable.

[← Volver al README](../readme.md) · [Chat y recuerdos](./chat-y-memorias.md) · [Guía de Chrome para WSL](../deploy/wsl/WEB_BROWSER.md)

## Razonamiento y Deep Think

**Endpoints** separa `llm.chat_reasoning_effort` del esfuerzo del planificador web (`llm.web_reasoning_effort`). Ambos empiezan en Off (`none`); se puede elegir Off, Auto, Low, Medium, High o Max. Auto omite el parámetro; los otros valores se envían como `reasoning_effort` o `reasoning.effort`. Max se adapta según familia del modelo ([Strata 0.2.9](https://github.com/Niko1221/Strata/blob/v0.2.9/serve/frontend.py#L134-L169)); la reserva incluye pensamiento y respuesta ([documentación de Strata](https://github.com/Niko1221/Strata/blob/v0.2.9/docs/DETAILS.md#L469)). Esto no cambia el contrato de moderación.

**IA → Deep Think** (`ai_deep_think`) está apagado por defecto. Amplía el trabajo para preguntas complejas y, con Internet, investigación pública. Es independiente de `ai_web_enabled` y no permite enviar consultas privadas de permisos o moderación a la web.

## Investigación web opcional

`ai_web_enabled` activa búsqueda pública con SearXNG privado y lectura mediante Chrome Headless, solo con Strata. Está apagado por defecto. Requiere las dependencias e instalación descritas para [Docker/Coolify](../deploy/docker/README.md) o [WSL](../deploy/wsl/WEB_BROWSER.md), según el entorno. No usa el perfil ni sesiones del navegador del usuario; solo permite lectura pública, sin login, formularios ni acciones externas.

Cada consulta usa la API JSON de la instancia privada de SearXNG configurada por el operador. SearXNG reúne sus motores habilitados y puede devolver resultados aunque alguno falle. La petición HTTP y el filtrado comparten un plazo de 30 segundos; la respuesta JSON se limita a 1 MiB. Se validan URL y DNS públicos, se eliminan duplicados y solo los éxitos se guardan en caché durante cinco minutos. Una respuesta sin resultados públicos válidos produce un error de herramienta. Los diagnósticos DEBUG registran proveedor, categoría y cantidad, sin consultas, URLs ni mensajes crudos. El endpoint de infraestructura `SEARXNG_BASE_URL` no es un argumento del modelo y no permite al lector acceder a red privada.

### Flujo

El modo estándar y **Deep Think** son independientes. La búsqueda normal usa un límite configurable de fuentes y termina pronto si ya hay evidencia suficiente. Deep Think se invoca explícitamente para una sola consulta.

| Modo | Activación y límites |
| --- | --- |
| Búsqueda estándar | Activa **Internet** en IA. `ai_web_max_sources`: de 1 a 20 intentos de lectura por respuesta, predeterminado 3. Puede terminar antes si la respuesta está cubierta o alcanza el plazo configurado; dispone de hasta 2 búsquedas. Este límite no se aplica a Deep Think. |
| Tiempo estándar | **IA → Tiempo máximo de investigación estándar (segundos)** (`ai_web_timeout_seconds`): entero de 30 a 600, predeterminado 120. Incluye planificar, buscar y leer; no incluye la redacción final. El planificador dispone de hasta 30 segundos por llamada, el lector de 60 por página y la navegación de 30, siempre sujetos al tiempo total restante. Deep Think no tiene este plazo total. |
| Deep Think | Activa **Permitir Deep Think** y **Internet**; cada usuario usa `/deepthink consulta` para esa consulta. La configuración admite 0–1000 consultas Deep Think por persona y servidor al día, predeterminado 3; se reinicia a las 00:00 UTC. En 0 el comando queda deshabilitado. |
| Cuota global de investigación | No hay límite total de páginas, búsquedas, rondas o tiempo acumulado de Deep Think. El cupo diario del comando es independiente. |
| Tiempo por petición al modelo | Hasta 180 segundos en chat, 300 con Deep Think. No equivale a un plazo total de la investigación. Si Deep Think agota la salida, admite una recuperación compacta sin razonamiento adicional, con la misma reserva de salida y hasta 60 segundos; nunca publica el fragmento truncado. |
| Brief público | Tema de hasta 200 caracteres y pregunta autónoma de hasta 600. El planificador recibe solo ese brief, sin historial completo, perfiles, recuerdos, reglas ni metadatos de Discord. |
| Reparación del brief | Una llamada pública mal formada admite una sola reparación sin estado; si no produce un brief inequívoco y válido, no se envía la búsqueda. |
| Evidencia para la respuesta | Se usan pasajes de páginas realmente leídas; un extracto o URL descubierta no acredita una lectura. No se inventan URLs: tras iniciar la búsqueda solo se leen URLs explícitas del usuario, devueltas por un buscador o encontradas en una página leída/registro validado. Los enlaces de mensajes y adjuntos de Discord no se abren. |
| Grounding | La respuesta se redacta desde los pasajes, atribuye brevemente lo reportado por fuentes secundarias y no añade hechos, cifras ni fechas ausentes. No deduce aprobación, rechazo o estado oficial por falta de anuncio; acota la incertidumbre a ese punto. |
| Fuentes para el usuario | No añade bibliografía ni citas numeradas por defecto; incluye enlaces si se solicitan. Una cita/procedencia no garantiza por sí sola que la conclusión sea correcta o actual. |
| Reloj | Usa el UTC real después de esperas y validaciones; no infiere la zona horaria del usuario. |
| Trabajo temporal | Registra resumen público, evidencia, fuentes y referencias a mensajes, no páginas completas ni historial completo. Máximo 64 000 bytes por archivo, una hora de duración y hasta 64 archivos. Reutiliza solo en el mismo ámbito tras volver a comprobar configuración, permisos y mensajes originales. |

Deep Think reevalúa la suficiencia cada tres lecturas y tras tres lotes sin nuevos resultados materiales, además de compactar si llena el contexto. Puede seguir investigando cuando quedan lagunas relevantes. Cada checkpoint aceptado se guarda antes de continuar; reformular las mismas lagunas o releer el mismo pasaje no cuenta como avance. Si después agota el tiempo o la salida, la redacción conserva el último registro validado y expresa la incertidumbre en los puntos pendientes, sin presentar el trabajo como completo.

Deep Think contrasta fuentes independientes para afirmaciones volátiles, hipótesis alternativas y contradicciones materiales. Puede pedir precisión si falta un dato esencial; errores y lagunas no se presentan como una respuesta terminada. Para Chrome, revisa además los límites operativos y restricciones de lectura de la [guía WSL](../deploy/wsl/WEB_BROWSER.md).

### Diagnóstico de lectura

Buscar URLs y abrir páginas son fases independientes. El buscador puede funcionar aunque Chrome falte o no pueda iniciar su sandbox en el contenedor. Los fallos completos de lectura registran un WARNING administrativo con código, fase, estado HTTP e intentos, sin URL, consulta ni excepciones crudas; se conservan los errores estructurados que recibe el investigador. Consulta la [preparación de Docker/Coolify](../deploy/docker/README.md) para Chrome, usuario no root y permisos de volúmenes.

### Evidencia, privacidad y límites

El sistema contrasta fechas de publicación, modificación y del hecho con el reloj UTC. La antigüedad no descarta documentación oficial relevante. Las páginas y los resultados son datos no confiables, nunca instrucciones ni recuerdos. No investiga saludos, preferencias personales o consultas privadas de permisos/moderación.

### Citas y procedencia

El planificador conserva afirmaciones respaldadas, desconocidas o contradictorias. Solo citas literales de páginas leídas sirven de respaldo; si continúa en otra ronda, valida la misma pareja URL/cita. Resume el registro al llenar el contexto, sin reutilizar corpus o replay anterior. Antes de publicar, vuelve a comprobar permisos, cambios de configuración, borrados y fuentes multimedia.

### Notas temporales y continuidad

El trabajo temporal registra el resumen público, evidencia, fuentes y referencias a mensajes de origen, no páginas completas ni el historial completo. Vive en `catsbot-research/<servidor>/<canal>/<usuario>.md`, expira en una hora y se conservan hasta 64 archivos de un máximo de 64 000 bytes cada uno. Solo se reutiliza en el mismo ámbito tras revisar configuración, permisos y mensajes originales; los cambios, ediciones y borrados invalidan el trabajo. No es memoria global ni reemplaza verificar hechos volátiles.

### Operación, métricas y plantillas

| Área | Regla |
| --- | --- |
| Ritmo de peticiones | Strata mantiene concurrencia, cola y cooldown; `AI_STRATA_RATE_CAPACITY`/`AI_STRATA_RATE_PERIOD` permiten cuota propia. Capacidad 0 significa sin límite RPM adicional. Si se define `AI_RATE_CAPACITY`, Strata lo respeta salvo override específico; Gemini mantiene su limitador. |
| Permisos y fuentes | Las consultas independientes de permisos se agrupan dentro de cada validación; no se reutilizan permisos entre llamadas al proveedor y publicación. Captura pasiva del canal IA usa datos locales de Gateway y no exporta nada. Las referencias se verifican de nuevo antes de exportar contenido y antes de publicar; se conservan límites de profundidad, adjuntos y cambios de permisos. |
| Métricas | `listen_s`, `first_dispatch_s`, `queue_s` y `end_to_end_s` miden espera y recorrido; `first_dispatch_s` vale -1 si no hubo generación. Fases paralelas pueden solaparse; el tiempo del modelo incluye conexión y HTTP. Se registran solo números, nunca mensajes, claves, consultas ni URL. |
| Mensajes gestionados | Las plantillas mantienen sus valores por defecto; `ai_managed` por mensaje y avisos por categoría empiezan desactivados. Cada plantilla conserva su propia elección. |
| Redacción opcional | Usa Strata y recibe hasta 4000 caracteres del tono y hasta 12 000 de reglas, sin depender de los interruptores de chat/moderación. Conserva menciones, enlaces, datos de embeds y botones; sanciones, eliminaciones, timeouts, errores, confirmaciones privadas y citas mantienen su texto. |
| Fallback de redacción | Si falta configuración, la salida no valida, falla el proveedor o las dos plazas están ocupadas, se usa la plantilla original. Máximo 7 segundos por generación, sin cola. |

Las métricas de fases paralelas no deben sumarse como una secuencia. El tiempo del modelo mide transporte y HTTP, no solo inferencia.
