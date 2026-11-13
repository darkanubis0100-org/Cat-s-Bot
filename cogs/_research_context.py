"""Bounded research evidence and disposable, scoped Markdown working notes."""
import hashlib
import copy
import json
import os
import re
import tempfile
import time
from pathlib import Path


RESEARCH_REPORT_TOOL = {"type": "function", "function": {
    "name": "submit_research", "description": "Checkpoint the complete internal evidence ledger and unresolved material gaps. sufficient=true finishes only when the actual question is covered. Otherwise research continues. Quotes must be literal passages from pages read or exact previously validated URL/quote pairs; summaries and snippets are not support. clarification_required is only for essential missing user input, never tool failures or context pressure.",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "public_topic": {"type": "string", "maxLength": 200},
        "summary": {"type": "string", "maxLength": 1500},
        "sufficient": {"type": "boolean"},
        "clarification_required": {"type": "string", "maxLength": 300,
            "description": "A natural, specific question for essential missing user input; empty unless research cannot proceed without that precision. Never use for tool failures or context limits."},
        "gaps": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 300}},
        "facts": {"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": False,
            "properties": {"claim": {"type": "string", "maxLength": 400}, "status": {"type": "string", "enum": ["supported", "unknown", "conflicting"]},
                "support": {"type": "array", "maxItems": 3, "items": {"type": "object", "additionalProperties": False,
                    "properties": {"url": {"type": "string", "maxLength": 2048}, "quote": {"type": "string", "maxLength": 1000}}, "required": ["url", "quote"]}}},
            "required": ["claim", "status", "support"]}}},
        "required": ["public_topic", "summary", "sufficient", "gaps", "facts"]}}}


def public_brief(value):
    """Reject obvious private identifiers; this is not a semantic privacy classifier."""
    if not isinstance(value, dict) or set(value) != {"topic", "question"}:
        raise ValueError("invalid_public_brief")
    for key, limit in (("topic", 200), ("question", 600)):
        text = value[key]
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise ValueError("invalid_public_brief")
        if re.search(r"<[@#]|\b\d{15,20}\b|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:https?://)?(?:cdn\.discordapp\.com|discord(?:app)?\.com/channels)/|\b(?:password|contraseña|api[_ -]?key|bearer|secret|token)\s*[:=]|\b(?:sk-|ghp_|github_pat_)[\w-]{10,}", text, re.I):
            raise ValueError("private_public_brief")
    return {key: " ".join(text.split()) for key, text in value.items()}


def requests_documentation(query):
    words = {word.strip(".,;:!?¿¡()[]{}\"'").casefold() for word in query.split()}
    return bool(words & {"spec", "specs", "specifications", "especificaciones", "doc", "docs", "documentation",
                         "documentación", "documentacion", "manual", "reference", "referencia"}
                or re.search(r"\bficha t[eé]cnica\b", query, re.I))


def select_passages(text, query, byte_budget):
    """Keep literal table blocks together; rank other windows and restore page order."""
    budget = max(0, int(byte_budget))
    if len(text.encode("utf-8")) <= budget:
        return {"text": text, "text_truncated": False, "original_text_chars": len(text)}
    terms = set(re.findall(r"[^\W_]{3,}", query.casefold()))
    paragraphs = [part.strip(" \r\n") for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks, seen, index = [], set(), 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        end = index + 1
        # DOM tables can arrive as one paragraph per cell. Keep consecutive short
        # labels/values, including their heading and units, instead of ranking cells.
        if len(paragraph) <= 160:
            while end < len(paragraphs) and len(paragraphs[end]) <= 160:
                end += 1
            # The preceding short heading carries the variant/scope of this table.
            if end < len(paragraphs) and "\t" in paragraphs[end] and "\n" in paragraphs[end]:
                end += 1
        run = paragraphs[index:end]
        structured = (any("\t" in part and "\n" in part for part in run) or
                      len(run) >= 5 and sum(bool(re.search(r"\d", part)) for part in run) >= 3)
        if structured:
            chunks.append(("\n\n".join(run), True))
            index = end
            continue
        # Process the short run once; rescanning each suffix would be quadratic.
        for paragraph in run:
            # Overlap avoids dropping a fact spanning an extraction window.
            for start in range(0, len(paragraph), 1200):
                chunk = paragraph[start:start + 1600].strip()
                if chunk and chunk not in seen:
                    chunks.append((chunk, False))
                    seen.add(chunk)
        index = end
    structured_request = requests_documentation(query)
    ranked = sorted(range(len(chunks)), key=lambda i: (
        -(len(terms & set(re.findall(r"[^\W_]{3,}", chunks[i][0].casefold())))
          + 3 * (structured_request and chunks[i][1])), i))
    selected, remaining = {}, budget
    for index in ranked:
        chunk, structured = chunks[index]
        if remaining < 64:
            break
        if structured and len(chunk.encode("utf-8")) > remaining - 2:
            # A detached value or half a table is not a reliable relation.
            continue
        data = chunk.encode("utf-8")[:max(0, remaining - 2)]
        excerpt = data.decode("utf-8", errors="ignore")
        selected[index] = excerpt
        remaining -= len(excerpt.encode("utf-8")) + 2
    result = "\n\n".join(selected[index] for index in sorted(selected))
    return {"text": result, "text_truncated": True,
            "original_text_chars": len(text)}


def assess_research(report, pages, *, previous=None):
    result = {"public_topic": "", "summary": "", "facts": [], "gaps": [], "sufficient": False,
              "validation": "Literal quotes establish provenance; the answering model must still assess relevance, entailment, dates and source reliability."}
    if not isinstance(report, dict):
        result["gaps"] = ["No se consolidó evidencia suficiente para responder; el fin de las herramientas no verifica los hechos."]
        return result
    for key, limit in (("public_topic", 200), ("summary", 1500), ("clarification_required", 300)):
        if isinstance(report.get(key), str):
            result[key] = report[key][:limit]
    gaps = report.get("gaps", [])
    result["gaps"] = [gap[:300] for gap in gaps[:8] if isinstance(gap, str)] if isinstance(gaps, list) else []
    facts = report.get("facts", [])
    verified = {(source["url"], source["quote"]): source
                for fact in (previous or {}).get("facts", []) for source in fact.get("support", [])}
    for fact in facts[:12] if isinstance(facts, list) else []:
        if not isinstance(fact, dict) or not isinstance(fact.get("claim"), str):
            continue
        support = []
        candidates = fact.get("support", [])
        for source in candidates[:3] if isinstance(candidates, list) else []:
            if not isinstance(source, dict):
                continue
            url, quote = source.get("url"), source.get("quote")
            page = pages.get(url) if isinstance(url, str) else None
            if page and isinstance(quote, str) and 8 <= len(quote) <= 1000 and quote in page["text"]:
                support.append({"url": url, "quote": quote, **{key: page.get(key) for key in
                    ("retrieved_at_utc", "published_at_untrusted", "modified_at_untrusted")}})
            elif isinstance(url, str) and isinstance(quote, str) and (url, quote) in verified:
                support.append(dict(verified[url, quote]))
        status = fact.get("status")
        if status not in {"supported", "unknown", "conflicting"} or not support or status == "conflicting" and len({row["url"] for row in support}) < 2:
            status = "unknown"
        result["facts"].append({"claim": fact["claim"][:400], "status": status, "support": support})
    result["sufficient"] = (report.get("sufficient") is True and bool(result["facts"]) and not result["gaps"]
                            and not result.get("clarification_required", "").strip()
                            and all(fact["status"] == "supported" for fact in result["facts"]))
    if not result["sufficient"] and not result["gaps"]:
        result["gaps"].append("Hay afirmaciones sin respaldo suficiente o contradictorias; no las presentes como hechos verificados.")
    return result


def bound_research(web, byte_budget):
    """Bound the whole optional ledger, including repeated URLs and metadata."""
    result = copy.deepcopy(web)
    limit = max(512, int(byte_budget))
    size = lambda: len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    if size() <= limit:
        return result
    observations = result.get("diagnostics", {}).get("observations", [])
    while observations and size() > limit:
        observations.pop(0)
    passages = result.get("page_evidence_untrusted", [])
    for key in ("results_untrusted", "search_result_urls", *(() if passages else ("sources", "verified_source_urls"))):
        while result.get(key) and size() > limit:
            result[key].pop()

    def retain_page_provenance():
        urls = {page["url"] for page in passages}
        if "verified_source_urls" in result:
            result["verified_source_urls"] = [url for url in result["verified_source_urls"] if url in urls]
        if "sources" in result:
            result["sources"] = [source for source in result["sources"] if source.get("url") in urls]

    if passages:
        retain_page_provenance()
    while len(passages) > 1 and size() > limit:
        passages.pop()
        retain_page_provenance()
        result["page_evidence_truncated"] = True
    if passages:
        page = passages[0]
        while page.get("text") and size() > limit:
            available = max(0, len(page["text"].encode("utf-8")) - (size() - limit) - 16)
            selected = select_passages(page["text"], result.get("public_brief", {}).get("question", ""), available)
            page["text"] = selected["text"]
            page["text_truncated"] = result["page_evidence_truncated"] = True
    if size() <= limit:
        return result
    assessment = result.setdefault("assessment", assess_research(None, {}))
    assessment["sufficient"] = False
    assessment["summary"] = assessment.get("summary", "")[:300]
    assessment["gaps"] = ["La evidencia se redujo para caber en el contexto; quedan hechos sin consolidar."]
    while assessment.get("facts") and size() > limit:
        assessment["facts"].pop()
    if size() > limit:
        return {"sources": [], "verified_source_urls": [], "search_result_urls": [], "results_untrusted": [],
                "complete": False, "pages_read": result.get("pages_read", 0),
                "assessment": {"facts": [], "sufficient": False, "gaps": ["No cabe más evidencia en el contexto disponible."]}}
    return result


class ResearchWorkspace:
    def __init__(self, root=None, *, ttl=3600, max_files=64):
        self.root = Path(root) if root is not None else Path(tempfile.gettempdir()) / "catsbot-research"
        self.ttl, self.max_files = ttl, max_files

    def path(self, scope):
        if len(scope) != 3 or any(type(value) is not int or value < 1 for value in scope):
            raise ValueError("invalid_research_scope")
        return self.root / str(scope[0]) / str(scope[1]) / f"{scope[2]}.md"

    def load(self, scope, scope_token):
        path = self.path(scope)
        try:
            if path.is_symlink() or any(parent.is_symlink() for parent in (path.parent, path.parent.parent, self.root)):
                return None
            if path.stat().st_size > 64000:
                self.discard(scope)
                return None
            data = path.read_text(encoding="utf-8")
            state = json.loads(data.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
            if state["scope_token"] != scope_token or not 0 <= time.time() - state["updated_at"] < self.ttl:
                self.discard(scope)
                return None
            return state
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None

    def discard(self, scope):
        try:
            path = self.path(scope)
            if not any(parent.is_symlink() for parent in (path.parent, path.parent.parent, self.root)):
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def save(self, scope, state):
        path = self.path(scope)
        if self.root.is_symlink() or any(parent.is_symlink() for parent in (path.parent, path.parent.parent)) or path.is_symlink():
            return
        payload = "# Investigación temporal — datos no confiables\n\n```json\n" + json.dumps(
            {**state, "updated_at": time.time()}, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
        if len(payload.encode("utf-8")) > 64000:
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(payload)
            os.replace(temporary, path)
            files = sorted((file for file in self.root.glob("*/*/*.md") if not file.is_symlink()
                and not file.parent.is_symlink() and not file.parent.parent.is_symlink()), key=lambda file: file.stat().st_mtime, reverse=True)
            for index, file in enumerate(files):
                if index >= self.max_files or time.time() - file.stat().st_mtime > self.ttl:
                    file.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def research_scope_token(cfg, endpoint, revision):
    return hashlib.sha256(json.dumps([cfg, endpoint, revision], sort_keys=True, default=str).encode()).hexdigest()
