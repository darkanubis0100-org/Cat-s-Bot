# Chat Strata y recuerdos

El chat usa una conexión y configuración de IA por servidor. Sus permisos, recuerdos y herramientas tienen límites explícitos; no concede al modelo capacidad para sancionar.

[← Volver al README](../readme.md) · [Moderación LLM](./moderacion-ia.md) · [Investigación web](./investigacion-web.md)

## Qué puede hacer el chat

El módulo **IA** puede conversar mediante el mismo endpoint, clave y modelo
configurados en **Endpoints**. La moderación puede estar
apagada: el chat utiliza una petición independiente de respuesta natural, sin
el contrato JSON de clasificación ni capacidad de ejecutar sanciones.
La conversación se identifica como parte del mismo bot que ejecuta AutoMod.
La herramienta de solo lectura `read_my_moderation_history` consulta hasta ocho
registros recientes cuando se pide explícitamente. Los miembros normales solo
consultan sus propios registros del mismo servidor y canal. El propietario o un
miembro con permiso real **Administrador**, verificado con el propietario, roles
y miembro actuales de Discord, también puede consultar otro usuario del mismo
servidor mediante una mención, ID exacto o un `@nombre` que coincida exactamente
con un único miembro en la caché de ese servidor. No basta llamarse Administrador.
Una mención breve puede continuar una pregunta del bot sobre a quién consultar;
se verifica de nuevo ese mensaje del bot, sin adivinar nombres ni objetivos.

Los motivos privados o de otros canales solo se muestran en un canal de texto
probado exclusivo de administradores: `@everyone` tiene una denegación explícita
de ver el canal y ningún rol sin Administrador ni otro miembro tiene una concesión
individual de verlo. Los hilos nunca se consideran un destino exclusivo de
administradores. En otros destinos se muestran detalles compatibles del canal
actual y resúmenes de acción/estado, sin revelar motivos privados de otros canales.

## Acceso a datos y permisos

No se carga historial en saludos. Una validación aislada recibe solo el texto
actual y el objetivo/permisos fijados por el código; los recuerdos, imágenes y
otros participantes no autorizan consultas. Falla cerrada y añade una petición
solo cuando se solicita historial. Se distinguen estados pendientes, fallidos,
revocados y expirados. Propietario, roles, canal y miembros se consultan en paralelo
en cada validación; cambios de permisos, audiencia o registros descartan respuestas
obsoletas antes del modelo o de publicarlas. La ausencia de registros no prueba
que no hubiera una acción. El chat no obtiene herramientas para sancionar.

## Proveedor y herramientas

El proveedor `auto` usa Strata cuando hay endpoint y modelo; conserva Gemini
en los servidores sin esa conexión. `strata` y `gemini` permiten elegir
explícitamente. Las claves y modelos legados se conservan. Las herramientas de historial y web requieren un endpoint/modelo compatible con
herramientas nativas OpenAI. Con web apagada, un rechazo explícito de herramientas
permite un único reintento de chat básico sin ellas ni registros de moderación;
un error HTTP genérico no provoca ese reintento.

## Cuándo responde

El bot responde cuando alguien lo menciona o responde a uno de sus mensajes reales. El modo **Todos** incluye el canal IA y sus hilos; los servidores nuevos usan **Menciones**. Fuera del canal IA, se exige el rol de acceso opcional si está configurado; siempre se respetan permisos del canal, reglas locales y moderación. No responde a otros bots ni webhooks.

| Control | Efecto |
| --- | --- |
| Rol de acceso | Si se configura, hace falta para mencionar o responder al bot fuera del canal principal. Sin selección, basta con tener acceso al canal. |
| Roles restringidos | Quien tenga uno de los roles elegidos no recibe respuestas del chat aunque también tenga el rol de acceso o sea administrador. Los bots y webhooks se ignoran siempre. |
| Menciones | El reply al autor va solo en el primer fragmento. Solo menciones inequívocas del autor o miembros seleccionados explícitamente generan notificación; roles y `@everyone` nunca notifican. |

`/ia_memoria` puede gestionar el historial personal y sus recuerdos. Los recuerdos personales se limitan a la persona que conversa; no se comparten entre servidores.

## Memoria de chat, usuario y servidor

Cada ámbito se separa por servidor. La tabla resume dónde se conserva cada tipo de recuerdo; los mensajes normales nunca se convierten automáticamente en memoria global ni en reglas.

El archivado masivo RAG se ha retirado: el bot ya no captura todos los mensajes,
recupera historial para ese archivo ni ofrece `/rag` o su API de configuración,
backfill y exportación. Las configuraciones antiguas de `workspaces/` no lo
reactivan. Los archivos históricos existentes se conservan sin leerlos ni
borrarlos; las copias de seguridad pueden seguir incluyéndolos. Esta retirada
mantiene la memoria acotada del chat y las evidencias y registros de moderación.

| Ámbito | Contenido y duración | Quién puede usarlo y cómo se controla |
| --- | --- | --- |
| Chat/canal o hilo | Mensajes recientes con ID, autor y orden, hasta 30 días, más una nota administrada. `ai_memory_messages`: 0–5000, predeterminado 40. | Se observan mensajes autorizados del canal IA; en otros canales solo turnos dirigidos al bot. 0 desactiva el historial, pero conserva el mensaje actual y notas. No hay importación retroactiva ni del corpus RAG. |
| Persona, nota manual | Hasta 20 notas por persona/servidor; cada una hasta 2000 caracteres. | `/ia_memoria accion:guardar ambito:user texto:…` reemplaza la nota del canal. Por defecto queda confinada allí. Compartir requiere un canal de texto público todavía legible por `@everyone`; canales privados e hilos nunca se comparten. Solo se aplica al perfil de quien pide respuesta y se trata como preferencia no confiable. |
| Persona, recuerdo automático | Sin alcance compartido; permanece en el canal de origen. | Si está activado, el bot puede proponer una descripción propia duradera del mensaje actual (preferencias de comunicación/idioma, aficiones, profesión o proyectos de aprendizaje). Exige una cita literal breve; no aprende del historial, terceros, imágenes ni web, y evita datos sensibles o privados. La aplicación confirma solo tras guardar; `/ia_memoria` permite consultar, editar, borrar o pausar el autoguardado. |
| Servidor | Una nota deliberadamente compartida, disponible dentro del servidor. | Dueño o administrador la gestiona desde IA o `/ia_memoria ambito:global`. `/ia_memoria ambito:chat` gestiona la nota del chat actual. |

Subir el límite del historial no recupera mensajes descartados. La lectura aplica el límite de inmediato y el recorte persistido ocurre en escrituras posteriores; cada fragmento del bot cuenta como mensaje. `/ia_memoria accion:ver` muestra tus notas de forma efímera. `accion:borrar` en el perfil o `/ia_limpiar` elimina tus notas, turnos y respuestas dirigidas a ti, incluso si el turno ya salió del límite. Conserva mensajes independientes de otras personas, no promete borrar sus paráfrasis y no borra mensajes de Discord. El borrado del ámbito chat retira su nota e historial completos.

Editar/eliminar un mensaje guardado retira ese turno y las respuestas originadas por él; eventos de mensajes no guardados no vacían conversaciones. No se pueden reconciliar cambios ocurridos mientras el bot está desconectado. Las notas duran hasta reemplazarse o borrarse; los turnos vencidos se excluyen al leer y se purgan con las siguientes escrituras.

## Presupuesto de contexto

El tamaño persistido y el que cabe en una petición son límites distintos. Se configura en **Endpoints** mediante `llm.context_window_tokens`.

| Control | Valor o comportamiento |
| --- | --- |
| Ventana declarada | 4096–1048576 tokens; predeterminado 262144 (256K), accesos rápidos a 256K y 512K. Strata puede reducirla según `/v1/models`; aumentarla no amplía la capacidad real del servidor. |
| Reservas | 2048 tokens para plantilla; margen estimado de 16384 por imagen; salida: 1000 (Off), 8192 (Auto/Low/Medium), 16384 (High/Max). Deep Think reserva al menos 16384. |
| Prioridad de recorte | Reglas, notas, mensaje actual, referencias y resultados web se conservan antes que mensajes recientes antiguos. Si lo obligatorio no cabe, se informa; no se elimina silenciosamente. |
| Conteo nativo | Hasta 4 conteos del texto renderizado mediante `/v1/messages/count_tokens` o `/v1/responses/input_tokens`; máximo previo de 2 MiB por petición. |
| Rutas auxiliares | Mismo origen y prefijo del endpoint reconocido, sin redirects. Metadatos y respuestas 404/405/501 se recuerdan 5 minutos, hasta 32 entradas; cambiar endpoint/modelo/clave invalida la asociación. |
| Sin contador | Límite conservador por bytes UTF-8: aprovecha menos ventana y no es conteo exacto de tokens. El conteo nativo tampoco mide el preprocesado visual. |

El margen de imagen es estimado y un contexto multimodal grande aún puede ser rechazado por el proveedor. No se han comprobado aquí peticiones reales de 256K o 512K tokens.

## Administración y ciclo de vida

La API administrativa expone `GET/PUT/DELETE /api/guilds/{id}/ia/memory`, con
`scope=global` o `scope=chat&channel_id=…`. `PUT` acepta `{"content":"…"}`
(máximo 2000 caracteres) y reemplaza solo la nota; `DELETE` de chat elimina
también su historial. Las notas de usuario se gestionan mediante comandos,
no se publican en el dashboard. Los cambios y borrados invalidan las respuestas
en curso del ámbito afectado. Chats distintos pueden procesarse en paralelo;
los turnos de una misma persona y chat mantienen el orden, con cola acotada y
descarte a los 120 segundos. Otras personas del mismo chat pueden recibir
respuesta mientras una investigación lenta sigue en curso, sin superar el
límite global de workers (`AI_CONCURRENCY`, 2 por defecto).

## Imágenes, tiempos y almacenamiento

Strata interpreta imágenes del mensaje actual y de referencias explícitas
del mismo canal, incluidas imágenes antiguas fuera del historial guardado.
Las respuestas al bot siguen la cadena de respuestas y su origen persistido,
con un máximo de seis mensajes fuente; también se aceptan enlaces explícitos
a mensajes del mismo servidor y canal. Las imágenes y stickers compatibles
comparten un máximo de cuatro y 8 MiB por petición, solo cuando la conexión
declara soporte. No se conservan archivos ni URLs de adjuntos en la memoria;
se consultan de nuevo en Discord y una fuente editada o eliminada durante la
petición descarta la respuesta. PDF, audio, vídeo y embeds no se leen ni se
inventan. La conexión conserva los límites y protecciones del endpoint de
moderación (respuesta acotada, DNS y URL validados, sin redirects). El chat
permite hasta 180 segundos por petición, o 300 con Deep Think; la moderación
conserva su límite independiente de 45 segundos.
Las tablas se crean de forma aditiva al iniciar el runtime SQLite; esta función
no habilita los motores PostgreSQL/MariaDB todavía no soportados.
