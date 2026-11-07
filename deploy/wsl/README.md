# Cat's Bot en WSL

Guía de instalación y mantenimiento para una distribución WSL 2 dedicada.
Adapta el nombre de distribución, las rutas de Windows, el usuario y el dominio
antes de ejecutar los comandos. Esta guía no contiene el estado de una instalación.

## Estructura

| Elemento | Ubicación de referencia |
| --- | --- |
| Distribución WSL | `CatsBot` |
| Tarea de Windows | `CatsBot-WSL-Autostart` |
| Lanzador de Windows | `D:\WSL\CatsBot\host\Start-CatsBot.ps1` |
| Servicio Linux | `catsbot.service`, usuario `catsbot` |
| Aplicación / Python | `/opt/catsbot/app` / `/opt/catsbot/venv` |
| Credenciales | `/var/lib/catsbot/.env`, modo `0600` |
| Bases SQLite | `/var/lib/catsbot/data` |
| Archivos RAG históricos (función retirada) | `/var/lib/catsbot/workspaces` |
| Logs | `/var/log/catsbot/bot.log` y journal de systemd |

## Instalación

`provision.sh` prepara el usuario, los directorios y las dependencias del sistema.
`install-app.sh` instala un paquete en una aplicación nueva y rechaza sobrescribir
una instalación existente. Revisa sus argumentos antes de ejecutarlos. Configura
el entorno a partir de [`.env.example`](../../.env.example), rellena las claves
solo en el archivo privado y protege sus permisos. Para el navegador opcional,
consulta [WEB_BROWSER.md](WEB_BROWSER.md).

En PowerShell elevado, `Install-Autostart.ps1 -WindowsUser 'EQUIPO\usuario'`
registra la tarea de arranque. Indica `-HostDirectory` si usas otra ruta. WSL
registra las distribuciones por usuario: la tarea debe usar al propietario de la
distribución. S4U permite ejecutarla sin sesión interactiva ni contraseña guardada.
El lanzador mantiene WSL conectado y reintenta cuando la distribución termina;
systemd mantiene el servicio del bot. Verifica el arranque real en tu instalación.

## Administración desde PowerShell

```powershell
Get-ScheduledTask -TaskName 'CatsBot-WSL-Autostart'
wsl.exe -d CatsBot -u root --exec systemctl status catsbot.service --no-pager
wsl.exe -d CatsBot -u root --exec journalctl -u catsbot.service -n 80 --no-pager
wsl.exe -d CatsBot -u root --exec systemctl restart catsbot.service
```

Para detener temporalmente el bot, usa `systemctl stop catsbot.service`. Un nuevo
arranque de la distribución inicia el servicio habilitado. Para desactivar su
arranque automático, deshabilita la tarea de Windows y el servicio de forma
explícita. Nunca ejecutes otra copia de `main.py` con el mismo token.

## Actualizar y recuperar

Editar este repositorio no actualiza una instalación existente. Antes de actualizar,
detén el servicio y guarda la aplicación anterior y una copia del estado vivo.
`snapshot-state.py` copia `/var/lib/catsbot` a un directorio nuevo mediante SQLite
backup, sin importar ni ejecutar el bot. Conserva el `.env` con permisos privados.

Compila el panel con `npm ci --no-audit --no-fund` y `npm run build`, e instala las
dependencias Python en el entorno de Linux. Sustituye únicamente el código de
`/opt/catsbot/app`, conservando los enlaces `data`, `workspaces`, `.env` y `bot.log`.
Reinicia el servicio al terminar y comprueba su estado y los logs. Valida por
separado la API, el panel, Discord y el arranque tras reiniciar Windows.

Guarda paquetes, copias, registros y metadatos de despliegue en `deploy/wsl/.local/`,
que está excluido de Git. Una copia anterior no incluye los cambios posteriores:
para volver a Windows, detén WSL y copia el estado actual con SQLite backup antes
de ejecutar el Python del entorno `catsbot`. No ejecutes ambas copias a la vez.

Referencias: [systemd en WSL](https://learn.microsoft.com/en-us/windows/wsl/systemd),
[inicio de sesión de tareas](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)
y [red de WSL](https://learn.microsoft.com/en-us/windows/wsl/networking).
