import { useState, useEffect, useCallback, useRef } from "react";
import { apiGet, apiPatch, apiPut, apiDelete } from "../lib/api";
import { Icon } from "../lib/icons";
import { SearchableSelect } from "./ui";
import Toast from "./Toast";
import AIMessageCategories from "./AIMessageCategories";
import { useSaveBar } from "../lib/SaveBarContext";
import "./DashboardPanels.css";

export default function IAConfig({ selectedGuild: guildId, onNavigate }) {
  return <IASettings key={guildId} guildId={guildId} onNavigate={onNavigate} />;
}

function IASettings({ guildId, onNavigate }) {
  const [cfg, setCfg] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState(null);
  const [loading, setLoading] = useState(true);
  const [memoryChannelId, setMemoryChannelId] = useState("");
  const [chatDraftPending, setChatDraftPending] = useState(false);
  const requestVersion = useRef(0);

  const showToast = (msg, type = "success") => setToast({ msg, type });

  const load = useCallback(async () => {
    if (!guildId) return;
    const version = ++requestVersion.current;
    setLoading(true);
    setDirty(false);
    setToast(null);
    try {
      const iaCfg = await apiGet(`/api/guilds/${guildId}/ia`, { cache: false });
      if (version !== requestVersion.current) return;
      setCfg(iaCfg || {});
    } catch (e) {
      if (version !== requestVersion.current) return;
      showToast(e?.message || "Error cargando configuración IA", "error");
    } finally {
      if (version === requestVersion.current) setLoading(false);
    }
  }, [guildId]);

  useEffect(() => {
    load();
    return () => { requestVersion.current += 1; };
  }, [load]);

  const set = (key, val) => {
    setCfg((prev) => ({ ...prev, [key]: val }));
    setDirty(true);
  };
  const setId = (key) => (v) => set(key, v ? String(v) : null);

  const save = async () => {
    if (!cfg || saving || loading) return;
    for (const id of ["ia-web-max-sources", "ia-deep-think-daily-limit"]) {
      const input = document.getElementById(id);
      if (input && !input.reportValidity()) return;
    }
    const version = requestVersion.current;
    setSaving(true);
    try {
      // Solo enviamos columnas reales de ai_config para evitar el ValueError
      // que producía el viejo formulario al usar nombres como "model_name".
      const payload = {
        ai_enabled: (cfg.ai_enabled ?? 1) ? 1 : 0,
        ai_response_mode: cfg.ai_response_mode === "mentions" ? "mentions" : "all",
        ai_channel_id: cfg.ai_channel_id ?? null,
        ai_role_id: cfg.ai_role_id ?? null,
        ai_restricted_role_ids: cfg.ai_restricted_role_ids ?? [],
        ai_system_prompt: cfg.ai_system_prompt || null,
        ai_limit_requests: cfg.ai_limit_requests ?? 50,
        ai_limit_hours: cfg.ai_limit_hours ?? 12,
        ai_imagine_enabled: cfg.ai_imagine_enabled ? 1 : 0,
        ai_web_enabled: cfg.ai_web_enabled ? 1 : 0,
        ai_web_max_sources: cfg.ai_web_max_sources ?? 3,
        ai_deep_think: cfg.ai_deep_think ? 1 : 0,
        ai_deep_think_daily_limit: cfg.ai_deep_think_daily_limit ?? 3,
        ai_memory_messages: cfg.ai_memory_messages ?? 40};
      await apiPatch(`/api/guilds/${guildId}/ia`, payload);
      if (version !== requestVersion.current) return;
      setDirty(false);
      showToast("Configuración IA guardada");
    } catch (e) {
      if (version === requestVersion.current) showToast(e.message, "error");
    } finally {
      if (version === requestVersion.current) setSaving(false);
    }
  };

  useSaveBar({ dirty: dirty && !loading, saving, onSave: save, onRevert: load });

  if (loading && !cfg)
    return (
      <div className="dashboard-empty-state">
        <div className="loading-spinner" />
        <p>Cargando módulo IA…</p>
      </div>
    );

  if (!cfg)
    return (
      <div className="dashboard-empty-state">
        <Toast toast={toast} onDismiss={() => setToast(null)} />
        <p role="alert">No se pudo cargar la configuración IA.</p>
        <button className="btn-secondary" onClick={load}>Reintentar</button>
      </div>
    );

  const usingStrata = cfg?.ai_provider === "strata" || ((cfg?.ai_provider || "auto") === "auto" && cfg?.endpoint_configured);

  // Renderers para SearchableSelect.
  const renderChannelOption = (opt) => (
    <>
      <Icon name="channel" />
      <span className="ss-option-label">{opt.name}</span>
      {opt.category ? <span className="ss-option-sub">{opt.category}</span> : null}
    </>
  );
  const renderChannelSelected = (opt) => (
    <>
      <Icon name="channel" /> {opt.name}
    </>
  );
  const renderRoleOption = (opt) => (
    <>
      {opt.color ? (
        <span className="ss-swatch" style={{ background: opt.color }} aria-hidden="true" />
      ) : (
        <Icon name="role" />
      )}
      <span className="ss-option-label">{opt.name}</span>
    </>
  );

  return (
    <div className="ov-container animate-fade-in">
      <Toast toast={toast} onDismiss={() => setToast(null)} />

      <div className="section-header">
        <h2
          style={{
          }}
        >
          Inteligencia Artificial
        </h2>
        <p className="muted-text">
          Configura las conversaciones, las herramientas y la memoria del asistente.
        </p>
      </div>

      <div className="glass-panel" style={{ padding: 24, borderRadius: 22 }}>
        <h3 style={{ marginTop: 0 }}>Conexión: {usingStrata ? "Strata" : "Gemini"}</h3>
        <p className="muted-text">El proveedor, el modelo, la API key y la ventana de contexto se configuran en Endpoints. La conexión Strata también se comparte con Moderación IA.</p>
        {usingStrata && !cfg.endpoint_configured && <p role="status" className="muted-text">Configura el endpoint y el modelo para que Strata pueda responder.</p>}
        {cfg.effective_model && <p className="muted-text">Modelo: {cfg.effective_model}</p>}
        <button type="button" className="btn-secondary" disabled={saving || loading} onClick={() => onNavigate?.("endpoints")}>Configurar Endpoints</button>
      </div>

      <fieldset disabled={saving || loading || !cfg} style={{ border: 0, padding: 0, margin: 0, minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Estado del módulo */}
      <div
        className="glass-panel"
        style={{
          padding: 24,
          borderRadius: 22,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center"}}
      >
        <div>
          <h3 style={{ margin: 0 }}>Módulo de IA</h3>
          <p style={{ color: "var(--muted)", margin: "4px 0 0", fontSize: "0.84rem" }}>
            {(cfg?.ai_enabled ?? 1) ? "Activo y listo para responder" : "Desactivado"}
          </p>
        </div>
        <label className="toggle-switch">
          <input
            aria-label="Activar IA conversacional"
            type="checkbox"
            checked={!!(cfg?.ai_enabled ?? 1)}
            onChange={(e) => set("ai_enabled", e.target.checked ? 1 : 0)}
          />
          <span className="slider" />
        </label>
      </div>

      {/* Modelo + canales */}
      <div
        className="glass-panel"
        style={{ padding: 24, borderRadius: 22, display: "flex", flexDirection: "column", gap: 16 }}
      >
        <div className="section-title">
          <h3 style={{ margin: 0 }}>Canales y acceso</h3>
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,260px),1fr))",
            gap: 16}}
        >
          <div className="config-item" style={{ marginBottom: 0 }}>
            <label>Canal principal de IA (opcional)</label>
            <SearchableSelect
              disabled={saving || loading}
              value={cfg?.ai_channel_id || ""}
              onChange={setId("ai_channel_id")}
              endpoint={`/api/guilds/${guildId}/channels`}
              itemsKey="channels"
              placeholder="Todos los canales permitidos…"
              renderOption={renderChannelOption}
              renderSelected={renderChannelSelected}
            />
            <span style={{ fontSize: "0.76rem", color: "var(--text-muted)" }}>
              Sin selección, puede participar en los canales permitidos. El canal elegido y sus hilos usan el modo de respuesta configurado; fuera de ellos responde a menciones y respuestas directas.
            </span>
          </div>

          <div className="config-item" style={{ marginBottom: 0 }}>
            <label>Rol Ping (opcional)</label>
            <SearchableSelect
              disabled={saving || loading}
              value={cfg?.ai_role_id || ""}
              onChange={setId("ai_role_id")}
              endpoint={`/api/guilds/${guildId}/roles`}
              itemsKey="roles"
              placeholder="Sin restricción de rol…"
              renderOption={renderRoleOption}
              renderSelected={(opt) => (
                <>
                  {opt.color ? (
                    <span className="ss-swatch" style={{ background: opt.color }} aria-hidden="true" />
                  ) : (
                    <Icon name="role" />
                  )}{" "}
                  {opt.name}
                </>
              )}
            />
            <span style={{ fontSize: "0.76rem", color: "var(--text-muted)" }}>
              Si se elige, exige este rol para mencionar o responder al bot fuera del canal principal. Sin selección, basta con tener acceso al canal.
            </span>
          </div>
          <div className="config-item" style={{ marginBottom: 0 }}>
            <label>Roles restringidos (opcional)</label>
            <SearchableSelect
              ariaLabel="Roles restringidos para el chat de IA"
              disabled={saving || loading}
              multiple
              value={cfg?.ai_restricted_role_ids ?? []}
              onChange={(roles) => set("ai_restricted_role_ids", roles)}
              endpoint={`/api/guilds/${guildId}/roles?include_managed=true`}
              itemsKey="roles"
              placeholder="Seleccionar roles restringidos…"
              renderOption={renderRoleOption}
              renderSelected={renderRoleOption}
            />
            <span style={{ fontSize: "0.76rem", color: "var(--text-muted)" }}>
              Quien tenga uno de estos roles no recibirá respuestas del chat de IA, aunque tenga el rol de acceso o sea administrador. Los bots y webhooks se ignoran siempre.
            </span>
          </div>
        </div>
      </div>

      {/* Prompt + webhook */}
      <div
        className="glass-panel"
        style={{ padding: 24, borderRadius: 22, display: "flex", flexDirection: "column", gap: 14 }}
      >
        <div className="section-title">
          <h3 style={{ margin: 0 }}>Personalidad</h3>
        </div>

        <div className="config-item">
          <label htmlFor="ia-system-prompt">Prompt del sistema</label>
          <textarea
            id="ia-system-prompt"
            rows={6}
            value={cfg?.ai_system_prompt || ""}
            placeholder="Eres un asistente del servidor…"
            onChange={(e) => set("ai_system_prompt", e.target.value)}
            style={{
              width: "100%",
              resize: "vertical",
              padding: "10px 12px",
              background: "var(--panel)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius-md)",
              color: "var(--text)",
              fontFamily: "var(--font-main)",
              fontSize: "0.9rem"}}
          />
        </div>


      </div>

      <AIMessageCategories key={guildId} guildId={guildId} />

      {/* Limites + flags */}
      <div
        className="glass-panel"
        style={{ padding: 24, borderRadius: 22, display: "flex", flexDirection: "column", gap: 14 }}
      >
        <div className="section-title">
          <h3 style={{ margin: 0 }}>Límites y comportamiento</h3>
        </div>

        <div className="config-item inline-check">
          <div>
            <div style={{ fontWeight: 700 }}>Responder a todos los mensajes del canal</div>
            <div style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
              Desactívalo para responder cuando mencionen al bot o respondan a uno de sus mensajes.
            </div>
          </div>
          <label className="toggle-switch">
            <input
              aria-label="Responder a todos los mensajes del canal"
              type="checkbox"
              checked={(cfg?.ai_response_mode || "all") === "all"}
              onChange={(e) => set("ai_response_mode", e.target.checked ? "all" : "mentions")}
            />
            <span className="slider" />
          </label>
        </div>

        {usingStrata && <p className="muted-text">Con «El modelo admite imágenes» de Endpoints, Strata puede leer imágenes del mensaje actual y de mensajes citados o enlazados del mismo canal, según sus permisos, aunque estén fuera del historial almacenado. Máximo 4 imágenes y 8 MiB por consulta. Sin esa opción no descarga imágenes. No interpreta PDF, audio ni vídeo.</p>}

        {usingStrata && <div className="config-item inline-check">
          <div>
            <div style={{ fontWeight: 700 }}>Internet: DuckDuckGo, Yahoo y Chrome Headless</div>
            <div style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
              Permite buscar en la web pública y leer páginas para responder con fuentes. Se activa solo para la conversación, independientemente de Moderación IA.
            </div>
          </div>
          <label className="toggle-switch">
            <input type="checkbox" aria-label="Permitir búsqueda web y lectura de páginas públicas con Chrome"
              checked={!!cfg.ai_web_enabled} onChange={(e) => set("ai_web_enabled", e.target.checked ? 1 : 0)} />
            <span className="slider" />
          </label>
        </div>}
        {usingStrata && <p className="muted-text">La búsqueda web puede usar el contexto de la conversación para entender referencias como «busca sobre eso». Solo accede a páginas públicas: sin sitios privados o locales, inicios de sesión ni formularios. Desactivada, no usa estas herramientas web.</p>}

        {usingStrata && <div className="config-item">
          <label htmlFor="ia-web-max-sources">Máximo de fuentes · búsqueda estándar</label>
          <input id="ia-web-max-sources" type="number" min={1} max={20} step={1} required
            aria-describedby="ia-web-max-sources-help"
            value={cfg.ai_web_max_sources ?? 3}
            onChange={(e) => set("ai_web_max_sources", e.target.value === "" ? "" : Number(e.target.value))} />
          <span id="ia-web-max-sources-help" className="muted-text">De 1 a 20. Máximo de páginas que el bot intentará consultar por respuesta. Puede terminar antes si ya tiene suficiente información o alcanza los 30 segundos del modo estándar. No cambia Deep Think.</span>
        </div>}

        {usingStrata && <div className="config-item inline-check">
          <div><div style={{ fontWeight: 700 }}>Permitir Deep Think mediante /deepthink</div><div style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>Cada persona puede usar /deepthink consulta para investigar a fondo solo esa consulta; las conversaciones normales mantienen el modo rápido. Contrasta fuentes, alternativas y dudas relevantes hasta que el modelo considere suficiente la información. Puede tardar más y consumir más tokens y recursos del proveedor. Requiere tener Internet activado.</div></div>
          <label className="cr-switch"><input type="checkbox" aria-label="Permitir Deep Think mediante /deepthink" checked={!!cfg.ai_deep_think} onChange={(e) => set("ai_deep_think", e.target.checked ? 1 : 0)} /><span aria-hidden="true" /></label>
        </div>}
        {usingStrata && <div className="config-item">
          <label htmlFor="ia-deep-think-daily-limit">Consultas Deep Think diarias por usuario</label>
          <input id="ia-deep-think-daily-limit" type="number" min={0} max={1000} step={1} required
            value={cfg.ai_deep_think_daily_limit ?? 3}
            onChange={(e) => set("ai_deep_think_daily_limit", e.target.value === "" ? "" : Number(e.target.value))} />
          <span className="muted-text">De 0 a 1000 consultas por persona en este servidor. El cupo se reinicia a las 00:00 UTC. Con 0 se deshabilita /deepthink.</span>
        </div>}
        {usingStrata && !!cfg.ai_deep_think && !cfg.ai_web_enabled && <p role="status" className="muted-text">El permiso de /deepthink está guardado; el comando requiere activar Internet.</p>}

        {!usingStrata && <div className="config-item inline-check">
          <div>
            <div style={{ fontWeight: 700 }}>Multimodal (imágenes/PDF)</div>
            <div style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
              Permite a la IA leer adjuntos del usuario.
            </div>
          </div>
          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={!!cfg?.ai_imagine_enabled}
              onChange={(e) => set("ai_imagine_enabled", e.target.checked ? 1 : 0)}
            />
            <span className="slider" />
          </label>
        </div>}

        {!usingStrata && <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,240px),1fr))", gap: 14 }}>
          <div className="config-item" style={{ marginBottom: 0 }}>
            <label>Máx. requests por usuario</label>
            <input
              type="number"
              min="1"
              max="500"
              value={cfg?.ai_limit_requests ?? 50}
              onChange={(e) => set("ai_limit_requests", parseInt(e.target.value))}
            />
          </div>
          <div className="config-item" style={{ marginBottom: 0 }}>
            <label>Ventana del límite (horas)</label>
            <input
              type="number"
              min="1"
              max="72"
              value={cfg?.ai_limit_hours ?? 12}
              onChange={(e) => set("ai_limit_hours", parseInt(e.target.value))}
            />
          </div>
        </div>}
      </div>
      </fieldset>

      <div className="glass-panel" style={{ padding: 24, borderRadius: 22, display: usingStrata ? undefined : "none" }}>
        <h3 style={{ marginTop: 0 }}>Memoria persistente</h3>
        <p className="muted-text">Cat’s Bot conserva el historial reciente de cada chat hasta el límite configurado durante 30 días. En el canal principal recoge el contexto de la conversación; en otros canales guarda solo las interacciones dirigidas al bot.</p>
        <div className="config-item">
          <label htmlFor="ia-memory-messages">Mensajes de memoria por chat</label>
          <input id="ia-memory-messages" type="number" min={0} max={5000} step={1} value={cfg.ai_memory_messages ?? 40}
            disabled={saving || loading} onChange={(e) => set("ai_memory_messages", e.target.value === "" ? "" : Number(e.target.value))} />
          <span className="muted-text">Máximo de 0 a 5000 mensajes. Se guarda con la configuración del panel. Con 0 se desactiva solo el historial reciente: el mensaje actual y las notas personales, del chat y del servidor siguen disponibles.</span>
          <span className="muted-text">El contexto enviado puede usar menos mensajes para caber en la ventana de tokens del modelo y dejar espacio para instrucciones, imágenes, búsquedas web y respuesta. La retención sigue siendo de 30 días; aumentar el límite no recupera mensajes ya descartados.</span>
        </div>
        <p className="muted-text">Las notas de abajo se guardan por separado de la configuración.</p>
        <MemoryEditor key={`global-${guildId}`} guildId={guildId} scope="global" />
        <div className="config-item" style={{ marginTop: 24 }}>
          <label>Memoria de un chat</label>
          <SearchableSelect value={memoryChannelId} onChange={(value) => setMemoryChannelId(value ? String(value) : "")}
            disabled={chatDraftPending} endpoint={`/api/guilds/${guildId}/channels`} itemsKey="channels"
            placeholder="Elige un canal para gestionar su memoria…" renderOption={renderChannelOption} renderSelected={renderChannelSelected} />
          {chatDraftPending && <span className="muted-text">Guarda o descarta la nota del chat antes de cambiar de canal.</span>}
        </div>
        {memoryChannelId && <MemoryEditor key={`${guildId}-${memoryChannelId}`} guildId={guildId} scope="chat" channelId={memoryChannelId} onPendingChange={setChatDraftPending} />}
        <p className="muted-text" style={{ marginTop: 24 }}>Cada persona gestiona su propia memoria con <code>/ia_memoria</code> y la borra con <code>/ia_limpiar</code>. El bot puede guardar recuerdos personales útiles de lo que esa persona expresa en su mensaje actual y avisa al confirmar el guardado. Se pueden consultar, editar y borrar por ID, o pausar el guardado automático sin perder los existentes, desde ese comando. Los recuerdos automáticos siempre se quedan en el chat de origen. Las notas manuales solo acompañan al usuario a otros chats del mismo servidor si activa <code>compartir_en_servidor: True</code> al guardar, desde un canal público ordinario que siga siendo público. Las notas de canales privados y de hilos siempre se quedan en su origen. El bot usa solo la memoria de quien le habla; no comparte memoria entre servidores. Los recuerdos pueden aparecer en sus respuestas visibles.</p>
      </div>
    </div>
  );
}

function MemoryEditor({ guildId, scope, channelId, onPendingChange }) {
  const [content, setContent] = useState("");
  const [savedContent, setSavedContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const version = useRef(0);
  const dirty = content !== savedContent;
  const path = `/api/guilds/${guildId}/ia/memory?scope=${scope}${channelId ? `&channel_id=${encodeURIComponent(channelId)}` : ""}`;
  const title = scope === "global" ? "Memoria del servidor" : "Memoria del chat";

  const load = useCallback(async () => {
    const current = ++version.current;
    setLoading(true);
    setError("");
    try {
      const data = await apiGet(path, { cache: false });
      if (current !== version.current) return;
      setContent(data.content || "");
      setSavedContent(data.content || "");
      setLoaded(true);
    } catch (e) {
      if (current === version.current) setError(e.message || "No se pudo cargar la memoria");
    } finally {
      if (current === version.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    load();
    return () => { version.current += 1; };
  }, [load]);

  useEffect(() => {
    onPendingChange?.(dirty || saving);
  }, [dirty, saving, onPendingChange]);

  const write = async (remove = false) => {
    const current = version.current;
    setSaving(true);
    setError("");
    setFeedback("");
    try {
      const data = remove ? await apiDelete(path) : await apiPut(path, { content });
      if (current !== version.current) return;
      const next = remove ? "" : data.content;
      setContent(next);
      setSavedContent(next);
      setFeedback(remove ? (scope === "chat" ? "Memoria e historial almacenado del chat borrados. Los mensajes de Discord se conservan." : "Memoria del servidor borrada.") : `${title} guardada.`);
    } catch (e) {
      if (current === version.current) setError(e.message || "No se pudo guardar la memoria");
    } finally {
      if (current === version.current) setSaving(false);
    }
  };

  return (
    <div style={{ marginTop: 20 }}>
      <div className="config-item">
        <label htmlFor={`ia-memory-${scope}`}>{title}</label>
        <textarea id={`ia-memory-${scope}`} rows={4} maxLength={2000} value={content} disabled={loading || saving || !loaded}
          placeholder={scope === "global" ? "Reglas de conversación y datos compartidos del servidor…" : "Datos y contexto duradero de este chat…"}
          onChange={(e) => { setContent(e.target.value); setFeedback(""); }} />
        <span className="muted-text">{loading ? "Cargando memoria…" : `${content.length}/2000 caracteres`}. {scope === "global" ? "Disponible para las conversaciones de todo el servidor." : "Esta nota y su historial se usan solo en este canal."}</span>
      </div>
      {error && <p role="alert">{error} {loading ? null : <button className="btn-secondary" onClick={load} disabled={saving}>{dirty ? "Descartar y recargar nota" : "Volver a cargar"}</button>}</p>}
      {feedback && <p role="status">{feedback}</p>}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        <button className="btn-primary" onClick={() => write()} disabled={loading || saving || !loaded || !dirty}>Guardar {scope === "global" ? "memoria del servidor" : "memoria del chat"}</button>
        <button className="btn-secondary" onClick={() => { setContent(savedContent); setError(""); setFeedback(""); }} disabled={loading || saving || !dirty}>Descartar nota {scope === "global" ? "del servidor" : "del chat"}</button>
        <button className="btn-danger" onClick={() => write(true)} disabled={loading || saving || !loaded}>Borrar {scope === "global" ? "memoria del servidor" : "memoria e historial del chat"}</button>
      </div>
      {scope === "chat" && <p className="muted-text">Borrar elimina la nota y el historial guardado por el bot, sin borrar mensajes de Discord. Guardar una nota vacía elimina solo la nota.</p>}
    </div>
  );
}
