# Chrome en Docker y Coolify

## Búsqueda privada con Docker Compose

`docker-compose.yml` construye el bot con el Dockerfile existente y añade SearXNG como servicio privado. El bot conserva UID/GID `1000:1000`, su puerto interno `8080` y el perfil seccomp administrado en `/data/coolify/applications/1vq9mklpmnkkojoxragj06sr/chrome-seccomp.json`. El servicio `bot` se conecta a la red externa de la aplicación Coolify y a una red de búsqueda separada. Las etiquetas Traefik de Compose enrutan `https://mi.dark-anubis.com` únicamente al bot, puerto `8080`, y redirigen HTTP a HTTPS. Usan los entrypoints `http`/`https` y el resolver `letsencrypt` de la [configuración oficial de Coolify](https://coolify.io/docs/core/networking/proxy/traefik/overview). `searxng` solo se conecta a la red de búsqueda, tiene `traefik.enable=false`, no publica puertos ni recibe dominio. La red de búsqueda permite consultar Internet; no debe marcarse `internal: true` porque impediría esa salida.

La imagen auxiliar deriva de la [publicación oficial](https://github.com/searxng/searxng/pkgs/container/searxng), versión `2026.11.6-ac11e14c5`, fijada por digest de manifiesto `sha256:c750ee1d859b26d3aeae51c07f6a48c3a0bfbaa9195d71a18b258862d88c4c3c`. Incorpora `deploy/docker/searxng/settings.yml` en una ruta propia para que un volumen generado por la imagen base no oculte futuras actualizaciones de configuración. El arranque genera una clave de firma aleatoria efímera y ejecuta el entrypoint oficial; no hay sesiones de usuarios que conservar ni claves de API que contratar. El límite de peticiones de una instancia pública está desactivado; no necesita Redis/Valkey. El healthcheck local `/healthz` acredita que responde el servicio, no la disponibilidad de cada motor externo.

El bot usa `SEARXNG_BASE_URL=http://searxng:8080`, una dirección de infraestructura que no puede elegir el modelo. Consulta `POST /search` con `format=json`, habilitado explícitamente según la [API oficial](https://docs.searxng.org/dev/search_api.html). No sigue redirecciones del servicio, limita la respuesta a 1 MiB y comparte 30 segundos entre HTTP y filtrado. Se admiten respuestas parciales con motores caídos si conservan resultados públicos válidos. Los resultados pasan los filtros de URL/DNS, deduplicación y caché de éxitos existentes. La lectura Chrome conserva su frontera de red pública y sandbox.

Esta aplicación necesita **Raw Docker Compose**. En Coolify 4.6.0, el despliegue Compose administrado añade `env_file: .env` a cada servicio y expondría las credenciales del bot a SearXNG ([implementación de esa versión](https://github.com/coollabsio/coolify/blob/v4.6.0/app/Jobs/ApplicationDeploymentJob.php#L841)). Raw conserva las variables declaradas por servicio: el bot recibe sus claves explícitas y el buscador no recibe `.env` ni variables del bot. No intentes corregirlo vaciando una lista de secretos en el buscador.

Antes de cambiar Coolify, conserva sus variables actuales y configura los nombres de recursos existentes y el hostname sin esquema ni ruta:

```dotenv
CATBOT_DATA_VOLUME=1vq9mklpmnkkojoxragj06sr-catsbot-data
CATBOT_WORKSPACES_VOLUME=1vq9mklpmnkkojoxragj06sr-cat-s-botdark-v1-1vq9mklpmnkkojoxragj06sr-workspaces
CATBOT_PROXY_NETWORK=1vq9mklpmnkkojoxragj06sr
CATBOT_DOMAIN=mi.dark-anubis.com
```

Los dos volúmenes y la red del proxy están marcados `external: true`: Compose falla si faltan y no sustituye el estado por volúmenes vacíos. Conserva las variables de Discord, API, JWT, administrador y Gemini ya configuradas; el fichero no contiene sus valores.

Coolify 4.6.0 admite `${VAR}` y variables con valor predeterminado en nombres de volúmenes externos, pero rechaza el operador requerido `${VAR:?mensaje}` en ese campo. Por eso `x-required-config` exige los dos nombres antes de que Compose resuelva los campos `name: ${VAR}`. La comprobación falla tanto si la variable está ausente como si está vacía; no utiliza un nombre alternativo ni añade variables de infraestructura al entorno del bot. El validador de nombres de red sí admite el operador requerido y conserva su comprobación directa.

En la configuración de la aplicación establece `build_pack=dockercompose`, la ruta `/docker-compose.yml`, `is_raw_compose_deployment_enabled=true` e `inject_build_args_to_dockerfile=false`. Esto último impide que Coolify añada secretos de construcción al Dockerfile del buscador. Mantén los dominios gestionados de Compose vacíos: las etiquetas del fichero gestionan este hostname. Conserva la opción seccomp de la aplicación para que el helper pueda montar el perfil; el servicio bot también lo declara en Compose. Coolify añade sus etiquetas de administración; no se fijan IDs de contenedor manualmente. Comprueba las etiquetas y redes finales, que SearXNG no reciba variables del bot, la disponibilidad del perfil seccomp para el cliente Compose y los montajes reales antes de recrear el bot. `docker compose config --quiet` solo valida la configuración, no construye ni arranca servicios. La aceptación del despliegue requiere comprobar por separado búsqueda JSON real, lectura Chrome y estado persistente en ese entorno.

## Imagen del bot

El `Dockerfile` compila el panel y mantiene el runtime Python 3.12 de Debian Bookworm. Instala Google Chrome y sus dependencias con `python -m playwright install --with-deps chrome`, además del paquete Python de `requirements.txt`. El lector sigue usando `channel="chrome"` y `chromium_sandbox=True`; una búsqueda no necesita Chrome, pero leer los resultados sí.

La imagen ejecuta el bot como `catsbot`, UID/GID `1000:1000`. El código de `/app` conserva propietario root; solo `/app/bot.log`, `/app/data` y `/app/workspaces` se preparan como escribibles por el usuario del bot. El directorio HOME y los perfiles temporales también pertenecen a ese usuario. No instala navegadores durante el arranque ni cambia permisos de volúmenes automáticamente. `.dockerignore` evita copiar secretos, bases de datos, logs y paquetes de despliegue locales al contexto de la imagen.

## Preparación del entorno existente

Antes de desplegar esta imagen sobre un contenedor anterior que corría como root:

1. Identifica los volúmenes existentes montados en `/app/data` y `/app/workspaces`, conserva sus nombres y comprueba sus propietarios y permisos. No crees volúmenes vacíos para sustituirlos.
2. Conserva una copia consistente del estado antes de una migración de permisos. Comprueba que UID/GID `1000:1000` puede acceder a las carpetas y escribir tanto la base SQLite como sus archivos WAL/SHM y los workspaces existentes. Si hace falta migrar propietarios, hazlo de forma autorizada sobre esos volúmenes identificados; el `chown` del Dockerfile solo afecta a la capa de imagen y no arregla un volumen ya montado. No cambies propietarios de `/app` completo ni del código.
3. Comprueba que la política del host y del contenedor permite a Chrome crear su sandbox con un usuario no root. Si los namespaces de usuario están bloqueados por seccomp, AppArmor o el kernel, la instalación de Chrome por sí sola no basta. Usa una política revisada que permita los namespaces que Chrome necesita; no uses `--no-sandbox`, `--privileged`, `seccomp=unconfined` ni `CAP_SYS_ADMIN` para hacer pasar la lectura.
4. Comprueba espacio temporal y memoria compartida del contenedor para el navegador. El límite real del despliegue y la configuración de `/dev/shm` deben validarse en ese entorno; este Dockerfile no cambia opciones de ejecución de Coolify.

La documentación oficial de [Playwright en Docker](https://playwright.dev/python/docs/docker) describe el usuario separado y el perfil seccomp que permite `clone`, `setns` y `unshare` para el sandbox. La [guía de navegadores](https://playwright.dev/python/docs/browsers) explica la instalación del canal Chrome y las dependencias del sistema. Cualquier ajuste de política se aplica al runtime y requiere revisión para ese host; no se necesita acceso a la red privada ni cambiar los filtros de URL/DNS del bot.

## Perfil seccomp para Chrome

Para Docker Engine **29.9.3**, la [fuente oficial del motor](https://github.com/moby/moby/blob/docker-v29.9.3/go.mod#L78) fija `github.com/moby/profiles/seccomp v0.2.4`. Se usa su [perfil por defecto exacto](https://github.com/moby/profiles/blob/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json), añadiendo únicamente permiso para `clone`, `setns` y `unshare`, como recomienda Playwright para el sandbox. Se conservan `defaultAction=SCMP_ACT_ERRNO`, el resto de reglas y sus restricciones. No se sustituye por el perfil antiguo completo de Playwright, que difiere del default moderno en otras llamadas.

El perfil completo es un recurso operativo administrado por aplicación; Docker no hereda el default a partir de un fragmento de tres llamadas. Esta receta reproduce el JSON sin mantener una copia extensa del proveedor en el repositorio:

```python
import hashlib, json, urllib.request
from pathlib import Path

url = "https://raw.githubusercontent.com/moby/profiles/245180c51918481c0525424b3ee025d2b435d46c/seccomp/default.json"
with urllib.request.urlopen(url, timeout=10) as response:
    source = response.read(100001)
assert hashlib.sha256(source).hexdigest() == "785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334"
profile = json.loads(source)
profile["syscalls"].insert(0, {
    "comment": "Allow Chrome sandbox namespaces",
    "names": ["clone", "setns", "unshare"], "action": "SCMP_ACT_ALLOW",
    "args": [], "includes": {}, "excludes": {},
})
output = (json.dumps(profile, indent=2) + "\n").encode()
assert hashlib.sha256(output).hexdigest() == "43d3e9c825798859f9917e6b3548cb845dbaa6c11ccb16c74c1a178fa4d4150a"
Path("seccomp-chrome.json").write_bytes(output)
```

Antes de aplicarlo, compara estructuralmente con el default: retirar el primer bloque debe producir exactamente el JSON original. Una actualización de Docker requiere revisar su versión de `moby/profiles`; no se actualiza esta política automáticamente.

En Coolify 4.6.0, el perfil puede guardarse como archivo administrado bajo la configuración de la aplicación y referenciarse mediante `--security-opt seccomp=/ruta/absoluta/seccomp-chrome.json`. El helper monta readonly los perfiles absolutos reconocidos para que Compose pueda leerlos. La [implementación de Compose](https://github.com/docker/compose/blob/main/pkg/compose/create.go) lee el archivo desde el cliente y envía su JSON al daemon; una ruta relativa se resuelve contra `project.WorkingDir`, mientras que una absoluta se conserva. El archivo solo dentro de `/app` no acredita que el cliente Compose pueda abrirlo.

La comprobación controlada mantiene la misma imagen, usuario 1000 y demás opciones, cambiando solo ese perfil. Primero verifica el arranque de Chrome con `chromium_sandbox=True`; después prueba `WebAccess.read_webpage("https://example.com/")` y registra únicamente categoría, HTTP, tamaño y duración. Conservar el sandbox exige además que AppArmor y el kernel permitan los namespaces: si continúa `Operation not permitted`, el fallo no demuestra por sí solo que seccomp sea la única causa. No se cambia la política global del host, capacidades ni AppArmor para saltar esa comprobación.

## Comprobación después del despliegue

Verifica por separado:

- Imagen: Google Chrome y Playwright están instalados; el proceso usa UID/GID `1000:1000` y los volúmenes existentes son escribibles sin cambiar su contenido.
- Lectura: una página pública conocida devuelve `source_kind=page_read` y texto no vacío, usando el mismo usuario y restricciones del contenedor. Encontrar títulos y URLs con el buscador solo prueba la fase de búsqueda.
- Conversación: la evidencia leída llega al redactor; una respuesta del modelo por sí sola no prueba que se haya abierto una página.

Los fallos de lectura registran un WARNING con `code`, `stage`, `http_status` y `attempts`; no incluyen consultas, URLs, texto de la página ni excepciones crudas del navegador. `browser_launch_failed` apunta al inicio del navegador; `http_error` o `access_challenge` apuntan al sitio, y `read_timeout` incluye la fase que agotó el plazo. Se mantienen las comprobaciones de páginas públicas, redirecciones, subrecursos y el sandbox. El proxy admite hasta 16 conexiones simultáneas: rechaza la conexión que exceda ese pico sin marcar toda la página como agotada; los límites globales de 64 solicitudes y 8 MiB siguen deteniendo la lectura.

La validación local de Windows puede comprobar el contrato del lector con Chrome y una página HTTP sintética; no acredita los permisos de los volúmenes, el sandbox Linux ni las políticas de Docker/Coolify. Una imagen no construida y una prueba de lectura no ejecutada en el contenedor quedan pendientes; no deben declararse verificadas por pasar tests con mocks.
