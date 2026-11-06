<div align="center">

# Cat's Bot

<img src="./dashboard/public/logo.png" alt="Logo de Cat's Bot" width="150" />

![Cat's Bot: moderación, IA, radio y utilidades](https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=700&size=18&pause=1200&color=A855F7&center=true&vCenter=true&width=620&height=42&lines=Bot+para+Discord;Moderaci%C3%B3n+%E2%80%A2+IA+%E2%80%A2+Radio+%E2%80%A2+Utilidades;Las+Tortuguitas+de+Ezku)

[![Discord](https://img.shields.io/badge/Comunidad-Discord-5865F2?logo=discord&logoColor=white)](https://discord.com/invite/las-tortuguitas-de-ezku-1214414622111567892)
![Licencia GPL-3.0](https://img.shields.io/badge/Licencia-GPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/Bot-Python-3776AB?logo=python&logoColor=white)
![React](https://img.shields.io/badge/Panel-React-149ECA?logo=react&logoColor=white)

</div>

Cat's Bot reúne herramientas de moderación, conversación con IA, radio y gestión del servidor en un bot de Discord con panel web.

## Funciones

| Área | Qué incluye |
| --- | --- |
| 🛡️ Moderación | AutoMod, casos y configuración por servidor; moderación LLM opcional. |
| 💬 IA | Chat Strata con recuerdos por ámbito y recuerdos personales opcionales; Gemini se conserva como alternativa. |
| 🌐 Investigación | Búsqueda pública con DuckDuckGo/Yahoo y lectura mediante Chrome Headless. |
| 🎵 Radio | Reproducción de audio en canales de voz. |
| ⚙️ Panel | Configuración del bot y administración de funciones desde el navegador. |

## Arquitectura

| Componente | Tecnología | Responsabilidad |
| --- | --- | --- |
| Bot | Python, `discord.py` | Comandos, eventos y funciones de Discord. |
| API | FastAPI, Uvicorn | API REST y autenticación del panel. |
| Dashboard | React, Vite | Interfaz web de configuración. |
| Datos | SQLite | Configuración, casos y estado persistente del bot. |
| IA | Endpoint compatible con OpenAI; Gemini | Chat y moderación asistida según proveedor configurado. |
| Web opcional | DuckDuckGo/Yahoo, Playwright y Chrome Headless | Búsqueda pública y lectura de páginas. |

## Configuración y despliegue

1. Revisa [`.env.example`](./.env.example) para conocer las variables de entorno.
2. Para una instalación en WSL, sigue la [guía de despliegue](./deploy/wsl/README.md).
3. Para habilitar lectura web con Chrome, consulta la [guía del navegador](./deploy/wsl/WEB_BROWSER.md).

## IA y guías

| Función | Dónde se configura | Guía |
| --- | --- | --- |
| Moderación LLM | AutoMod y Endpoints; reglas y conexión por servidor. | [Protocolos, contexto y acciones](./docs/moderacion-ia.md) |
| Chat y recuerdos | IA y Endpoints; roles de acceso/restringidos y ámbitos de memoria. | [Respuestas, permisos y memoria](./docs/chat-y-memorias.md) |
| Investigación web | Internet, fuentes estándar (1–20; 3 por defecto), permiso Deep Think y cupo diario (3 por defecto). | [Flujo, evidencia y límites](./docs/investigacion-web.md) |

## Registros del servidor

En **Registros → Configuración de Logs**, activa **Cambios de canales y categorías (incluye permisos)** y guarda. Usa el canal de Server Logs y el interruptor general ya existentes. Registra ajustes de texto, voz, categorías y foros con valores antes/después, además de permisos por rol o usuario añadidos, eliminados o modificados: **permitir**, **denegar** y **heredar** (sin permiso explícito).

Los cambios extensos se dividen en varios mensajes; los textos largos se indican con `…`. No se envían menciones ni se atribuye un autor o una razón sin evidencia. Se conservan las configuraciones anteriores de `channel_updates`; un valor explícito de `channel_update` tiene prioridad. Requiere que el bot vea los canales y pueda enviar embeds al canal de logs. Los cambios anteriores a la activación no se reconstruyen.

**Roles: asignación, retirada y definición** registra altas/bajas de roles de miembros, incluso roles superiores al bot o gestionados, y creación/edición/eliminación de roles. Los apodos tienen su propio control. Con el intent de moderación y **Ver registro de auditoría**, roles y apodos usan la auditoría de Discord (también para miembros fuera de caché); sin esa capacidad, usan cambios Gateway de miembros cacheados. Registrar no concede capacidad para asignar esos roles.

Activa **Auditoría administrativa adicional** para ajustes del servidor, expulsiones/depuraciones, bots añadidos, emojis/stickers, invitaciones, webhooks/integraciones, hilos, eventos programados, onboarding, AutoMod, mensajes fijados y otras acciones que Discord publique en su auditoría. Incluye autor, objetivo e ID de auditoría cuando Discord los proporciona; los atributos compatibles muestran antes/después, y los demás solo su nombre, sin volcar secretos del SDK. Los controles existentes de mensajes, entradas/salidas, bans, voz y canales siguen independientes. Las configuraciones explícitas anteriores no activan automáticamente el nuevo control; las nuevas con valores por defecto sí. Los valores canónicos explícitos desactivados prevalecen sobre aliases antiguos, también al guardar desde `/serverlogs`.

La cobertura depende de los eventos, intents, permisos y visibilidad que Discord permita: no reconstruye actividad offline ni garantiza acciones que Discord no exponga. Las ediciones y eliminaciones de mensajes fuera de caché registran sus IDs y el nuevo texto disponible, sin inventar contenido anterior; las ediciones sin texto no tienen ese fallback. No registra cada mensaje enviado, reacción o cambio de presencia.

## Comunidad y contribuidores

Cat's Bot se desarrolla para la comunidad Las Tortuguitas de Ezku.

<div align="center">

**Leimsoto** <br/>
**Arielteen** <br/>
**Claude AI** <br/>
**DarkAnubis0100**

</div>

## Licencia

[GNU General Public License v3.0](./LICENSE)

<div align="center">

![Cat's Bot](https://capsule-render.vercel.app/api?type=waving&color=0d001a,7c3aed,ec4899&height=90&section=footer)

</div>
