# Chrome Headless opcional para las herramientas web

El acceso web se activa por servidor en el panel de IA. DuckDuckGo y Yahoo usan `ddgs`
primero con `duckduckgo` y, solo ante error o ausencia de resultados públicos válidos, con `yahoo` (sin selección `auto`). Cada intento dispone de hasta 6 segundos, dentro de los 15 segundos de la herramienta; el fallback no consume otra consulta del planificador. La lectura usa Google Chrome instalado, por el canal
`chrome` de Playwright. No se sustituye por Chromium ni se instala un navegador
al arrancar el bot. Los errores de instalación, sandbox y bloqueo del sitio se
devuelven como errores de herramienta. Las páginas y búsquedas no reciben la
memoria completa del chat ni credenciales del proveedor IA.
Para una instalación no estándar de Chrome, el operador puede indicar su ruta
absoluta en `CATBOT_CHROME_EXECUTABLE`; no se usa ningún navegador alternativo.

Preparación manual, **solo cuando el operador autorice la instalación**:

```bash
# Como root; dentro de la distribución CatsBot. Instala dependencias del SO y
# el canal estable de Google Chrome del proveedor. No arranca/reinicia el bot.
/opt/catsbot/venv/bin/python -m playwright install-deps chrome
/opt/catsbot/venv/bin/python -m playwright install chrome
```

Las dependencias Python de `requirements.txt` se incorporan mediante el flujo
normal de candidato y despliegue. No usar el Python global ni ejecutar otra
instancia de `main.py` para comprobar el navegador.

El servicio conserva `User=catsbot`, `NoNewPrivileges=true`, `ProtectHome=true`
y `ProtectSystem=strict`. Chrome usa su sandbox y un perfil temporal vacío;
no lee el perfil personal, cookies, extensiones ni descargas. Debe funcionar
el sandbox de namespaces de usuario del kernel y sus políticas AppArmor; si
el sistema lo impide, mantener el acceso web desactivado hasta que el operador
lo resuelva con la política adecuada. No añadir `--no-sandbox` ni relajar el
servicio para hacer pasar una prueba. Verificar con la identidad y restricciones
reales del servicio antes de declarar listo el entorno de producción.

Cada lectura inicia Chrome headless y un proxy temporal en `127.0.0.1` con puerto
aleatorio. El proxy resuelve cada destino, rechaza cualquier respuesta DNS no
pública y conecta directamente a la IP validada. Chrome usa ese proxy para
HTTP(S), incluidos redirecciones y subrecursos; desactiva QUIC, DNS directo y
UDP WebRTC fuera del proxy. Los formularios, métodos distintos de GET/HEAD,
websockets, service workers, popups y descargas se bloquean. No existe excepción
de red privada, tampoco a través de `CATBOT_LLM_ALLOWED_HOSTS`.

El contexto usa este User-Agent explícito, sin modificar otras propiedades del
navegador ni importar cookies/perfiles:

```text
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36
```

La navegación obtiene primero la respuesta HTTP y después espera texto útil y
estabilidad del DOM durante una ventana acotada. La extracción usa una copia de
`main`, `article` o el cuerpo, preserva separadores de bloques/tablas y no elimina
scripts del documento vivo. Puede incluir texto oculto en el DOM; no promete
representar solo el contenido visible. Una captura aún cambiante se marca
`partial_dom`. Si JavaScript cambia el documento, se comprueba la respuesta HTTP
del documento final, incluidos sus errores.

Límites: una operación web simultánea, 300 caracteres por búsqueda, cinco
resultados de búsqueda, hasta 128.000 bytes UTF-8 de texto seleccionado por página
(el planificador asigna normalmente hasta 32.000 o 64.000 según su presupuesto), 20 segundos por lectura,
8 MiB transferidos y 64 solicitudes/conexiones por página. Sitios con CAPTCHA,
autenticación, antibots, otros puertos o grandes recursos pueden fallar; no se
intenta eludir esos bloqueos. Se acepta contenido HTTP público como texto no
confiable. El proxy y el sandbox del navegador cubren navegación normal; no
sustituyen aislamiento de red del SO frente a una vulnerabilidad de Chrome.

Se permite una sola repetición para errores transitorios de navegación/red y
HTTP 502/503/504, únicamente si queda tiempo. Comparte los mismos 20 segundos,
8 MiB y 64 solicitudes del intento inicial. HTTP 401/403/429 y comprobaciones de
acceso no se repiten. Cambiar el User-Agent no garantiza que un sitio acepte la
lectura. Los errores devuelven categoría, etapa, estado HTTP cuando existe,
intentos y siguiente acción; nunca stacks ni comandos de Chrome. El planificador
busca otra fuente pública o declara la laguna, y no cita esos fallos como páginas
leídas. Sus categorías y límites se guardan en la nota temporal de investigación
existente, separados del resumen del modelo y con su mismo TTL y ámbito.

Pruebas aisladas de desarrollo, sin Discord ni base de datos:

```bash
/opt/catsbot/venv/bin/python -m unittest tests.test_web_access
# Añade fixture HTTP sintética + JavaScript en Chrome real y sandbox activo:
CATBOT_WEB_BROWSER_TEST=1 /opt/catsbot/venv/bin/python -m unittest tests.test_web_access
```

Las fixtures conectan únicamente a su servidor HTTP temporal de pruebas mediante
una sustitución explícita del transporte; el código de producción no incorpora
esa excepción. Las pruebas ordinarias comprueban el DNS público y la conexión
numérica fijada por separado. Una prueba pasada en Windows no confirma el
sandbox de Linux, el servicio ni la disponibilidad de los buscadores en WSL.

Documentación: [Chrome Headless](https://developer.chrome.com/docs/automation-and-testing/headless),
[canales de navegador de Playwright](https://playwright.dev/python/docs/browsers).
