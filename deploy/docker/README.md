# Chrome en Docker y Coolify

El `Dockerfile` compila el panel y mantiene el runtime Python 3.12 de Debian Bookworm. Instala Google Chrome y sus dependencias con `python -m playwright install --with-deps chrome`, además del paquete Python de `requirements.txt`. El lector sigue usando `channel="chrome"` y `chromium_sandbox=True`; una búsqueda no necesita Chrome, pero leer los resultados sí.

La imagen ejecuta el bot como `catsbot`, UID/GID `1000:1000`. El código de `/app` conserva propietario root; solo `/app/bot.log`, `/app/data` y `/app/workspaces` se preparan como escribibles por el usuario del bot. El directorio HOME y los perfiles temporales también pertenecen a ese usuario. No instala navegadores durante el arranque ni cambia permisos de volúmenes automáticamente. `.dockerignore` evita copiar secretos, bases de datos, logs y paquetes de despliegue locales al contexto de la imagen.

## Preparación del entorno existente

Antes de desplegar esta imagen sobre un contenedor anterior que corría como root:

1. Identifica los volúmenes existentes montados en `/app/data` y `/app/workspaces`, conserva sus nombres y comprueba sus propietarios y permisos. No crees volúmenes vacíos para sustituirlos.
2. Conserva una copia consistente del estado antes de una migración de permisos. Comprueba que UID/GID `1000:1000` puede acceder a las carpetas y escribir tanto la base SQLite como sus archivos WAL/SHM y los workspaces existentes. Si hace falta migrar propietarios, hazlo de forma autorizada sobre esos volúmenes identificados; el `chown` del Dockerfile solo afecta a la capa de imagen y no arregla un volumen ya montado. No cambies propietarios de `/app` completo ni del código.
3. Comprueba que la política del host y del contenedor permite a Chrome crear su sandbox con un usuario no root. Si los namespaces de usuario están bloqueados por seccomp, AppArmor o el kernel, la instalación de Chrome por sí sola no basta. Usa una política revisada que permita los namespaces que Chrome necesita; no uses `--no-sandbox`, `--privileged`, `seccomp=unconfined` ni `CAP_SYS_ADMIN` para hacer pasar la lectura.
4. Comprueba espacio temporal y memoria compartida del contenedor para el navegador. El límite real del despliegue y la configuración de `/dev/shm` deben validarse en ese entorno; este Dockerfile no cambia opciones de ejecución de Coolify.

La documentación oficial de [Playwright en Docker](https://playwright.dev/python/docs/docker) describe el usuario separado y el perfil seccomp que permite `clone`, `setns` y `unshare` para el sandbox. La [guía de navegadores](https://playwright.dev/python/docs/browsers) explica la instalación del canal Chrome y las dependencias del sistema. Cualquier ajuste de política se aplica al runtime y requiere revisión para ese host; no se necesita acceso a la red privada ni cambiar los filtros de URL/DNS del bot.

## Comprobación después del despliegue

Verifica por separado:

- Imagen: Google Chrome y Playwright están instalados; el proceso usa UID/GID `1000:1000` y los volúmenes existentes son escribibles sin cambiar su contenido.
- Lectura: una página pública conocida devuelve `source_kind=page_read` y texto no vacío, usando el mismo usuario y restricciones del contenedor. Encontrar títulos y URLs con el buscador solo prueba la fase de búsqueda.
- Conversación: la evidencia leída llega al redactor; una respuesta del modelo por sí sola no prueba que se haya abierto una página.

Los fallos de lectura registran un WARNING con `code`, `stage`, `http_status` y `attempts`; no incluyen consultas, URLs, texto de la página ni excepciones crudas del navegador. `browser_launch_failed` apunta al inicio del navegador; `http_error` o `access_challenge` apuntan al sitio, y `read_timeout` incluye la fase que agotó el plazo. Se mantienen las comprobaciones de páginas públicas, redirecciones, subrecursos y el sandbox.

La validación local de Windows puede comprobar el contrato del lector con Chrome y una página HTTP sintética; no acredita los permisos de los volúmenes, el sandbox Linux ni las políticas de Docker/Coolify. Una imagen no construida y una prueba de lectura no ejecutada en el contenedor quedan pendientes; no deben declararse verificadas por pasar tests con mocks.
