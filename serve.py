#!/usr/bin/env python3
"""Memory Map — servidor localhost que visualiza as memórias do Claude Code.

Lê ao vivo as 3 fontes de memória de um projeto:
  - ~/.claude/CLAUDE.md   (global do usuário)
  - ./CLAUDE.md           (projeto / time, versionado)
  - ./MEMORY.md           (acumulada pelo agente)
agrupa por seção markdown (## ou ###) -> tópico, cada bullet -> folha, e serve um
mapa mental interativo. Sem dependências além da stdlib.

Uso: python3 serve.py [porta]
"""
import re
import sys
import json
import pathlib
import functools
import unicodedata
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HOME = pathlib.Path.home()
CLAUDE_DIR = (HOME / ".claude").resolve()
GLOBAL_MD = CLAUDE_DIR / "CLAUDE.md"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATE = (PLUGIN_DIR / "template.html").read_text(encoding="utf-8")


def search_norm(s):
    """minúsculas + remove diacríticos — espelha o norm() do front (PT-friendly)."""
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if not unicodedata.combining(c))


def search_matcher(q):
    """substring por padrão; '*'/'?' viram glob não-ancorado. q já vem normalizado."""
    if "*" in q or "?" in q:
        body = re.escape(q).replace(r"\*", ".*").replace(r"\?", ".")
        try:
            rx = re.compile(body)
            return lambda s: rx.search(s) is not None
        except re.error:
            pass
    return lambda s: q in s


_content_cache = {}


def search_file(ref):
    """conteúdo normalizado de um arquivo de memória, cache por mtime; None se fora de ~/.claude."""
    rp = pathlib.Path(ref)
    if not (str(rp).startswith(str(CLAUDE_DIR)) and rp.is_file()):
        return None
    mt = rp.stat().st_mtime
    hit = _content_cache.get(ref)
    if not hit or hit[0] != mt:
        hit = (mt, search_norm(rp.read_text(encoding="utf-8", errors="replace")))
        _content_cache[ref] = hit
    return hit[1]


def clean(s):
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)          # [[wikilink]] -> wikilink
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)     # [txt](url)   -> txt
    s = s.replace("`", "")                              # inline code
    s = re.sub(r"\*\*?", "", s)                         # **bold** / *italic*
    return re.sub(r"\s+", " ", s).strip()               # mantém _ (identificadores)


IMPORT_RE = re.compile(r"^\s*@([^\s`]+)")                # @path no início da linha = import do Claude Code


def resolve_import(spec, base_dir):
    """Resolve um `@path` (~, absoluto ou relativo ao arquivo). Path de arquivo existente ou None."""
    spec = spec.strip().rstrip(".,;:)")                  # ponytail: tolera pontuação encostada
    try:
        if spec.startswith("~"):
            p = pathlib.Path(spec).expanduser()
        elif spec.startswith("/"):
            p = pathlib.Path(spec)
        else:
            p = base_dir / spec
        p = p.resolve()
    except Exception:
        return None
    return p if p.is_file() else None


def parse_md(path, base=None, _seen=None, _depth=0):
    """Retorna (topics, chars). topics = [{name, items:[{text, ref?}], imp?}]; chars = total
    de caracteres CARREGADOS. `base` ativa captura de ref (link .md). Expande os `@imports` do
    Claude Code (recursivo, profundidade <= 5, guarda de ciclo): os tópicos importados ganham
    `imp` (o spec escrito) e seus chars somam no total — esse texto também entra no contexto."""
    p = pathlib.Path(path).expanduser()
    if _seen is None:
        _seen = set()
    try:
        rp = p.resolve()
    except Exception:
        return [], 0
    if not p.exists() or rp in _seen:                    # ciclo / já contado
        return [], 0
    _seen.add(rp)
    text = p.read_text(encoding="utf-8", errors="replace")
    chars = len(text)
    topics, cur, fence = [], None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if _depth < 5:                                   # @import (fora de fence)
            mi = IMPORT_RE.match(line)
            if mi:
                imp = resolve_import(mi.group(1), p.parent)
                if imp is not None:
                    sub_topics, sub_chars = parse_md(imp, None, _seen, _depth + 1)
                    chars += sub_chars
                    for t in sub_topics:
                        t.setdefault("imp", mi.group(1))
                    topics.extend(sub_topics)
                cur = None
                continue
        h = re.match(r"^(#{2,3})\s+(.*\S)", line)        # ## ou ### = tópico
        if h:
            cur = {"name": clean(h.group(2)), "items": []}
            topics.append(cur)
            continue
        b = re.match(r"^\s*[-*]\s+(.*\S)", line)          # bullet = folha
        if b and cur is not None:
            txt = clean(b.group(1))
            if not txt:
                continue
            item = {"text": txt}
            if base is not None:
                m = re.search(r"\]\(([^)]+\.md)\)", line)
                if m:
                    ref = (pathlib.Path(base) / m.group(1)).resolve()
                    if ref.exists():
                        item["ref"] = str(ref)
            cur["items"].append(item)
    return [t for t in topics if t["items"]], chars


def make_source(b, file_label, role, tag, path, base=None):
    topics, chars = parse_md(path, base)
    return {"b": b, "file": file_label, "role": role, "tag": tag,
            "topics": topics, "chars": chars, "tokens": round(chars / 4)}  # ponytail: ~chars/4 (sem tiktoken)


def enc_path(p):
    """Codifica um path como o Claude Code faz no nome do dir de projeto (/ e . viram -)."""
    return re.sub(r"[/.]", "-", str(pathlib.Path(p).resolve()))


@functools.lru_cache(maxsize=None)
def proj_root(enc):
    """Recupera o cwd real de um projeto a partir do campo `cwd` dos transcripts .jsonl.
    O nome do dir codificado (/ e . viram -) é irreversível; o cwd gravado nas sessões é a
    fonte confiável. Retorna Path existente ou None. (cwd de um projeto é imutável -> cache.)"""
    d = PROJECTS_DIR / enc
    if not d.is_dir():
        return None
    for s in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with s.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:                       # ponytail: para na 1a linha com cwd
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except Exception:
                        continue
                    if cwd:
                        root = pathlib.Path(cwd).expanduser()
                        return root if root.exists() else None
        except Exception:
            continue
    return None


def discover():
    """Monta a lista de projetos. O projeto atual (cwd) vem primeiro. Cada projeto ganha
    global + CLAUDE.md do repo (quando resolvível) + MEMORY.md. O path real do repo vem do
    campo `cwd` dos transcripts .jsonl, já que o nome do dir codificado é irreversível."""
    cwd = pathlib.Path.cwd().resolve()
    cwd_enc = enc_path(cwd)
    mems = {}
    if PROJECTS_DIR.exists():
        for m in sorted(PROJECTS_DIR.glob("*/memory/MEMORY.md")):
            mems[m.parent.parent.name] = m
    order = ([cwd_enc] if cwd_enc in mems else []) + [e for e in mems if e != cwd_enc]

    projects = []
    for e in order:
        mem = mems[e]
        root = cwd if e == cwd_enc else proj_root(e)
        if root is not None:
            name = root.name
            dirlabel = str(root).replace(str(HOME), "~")
        else:  # repo não resolvível (sem transcript ou movido): cai no nome do dir codificado
            name = e.split("-Code-")[-1] if "-Code-" in e else e.strip("-").split("-")[-1]
            dirlabel = "~/.claude/projects/" + name
        sources = [make_source(0, "~/.claude/CLAUDE.md", "global do usuário", "user", GLOBAL_MD)]
        if root is not None and (root / "CLAUDE.md").exists():
            sources.append(make_source(1, "./CLAUDE.md", "projeto / time", "claude", root / "CLAUDE.md"))
        sources.append(make_source(2, "./MEMORY.md", "acumulada pelo agente", "memory", mem, base=mem.parent))
        projects.append({"name": name, "dir": dirlabel, "sources": sources})

    if not projects:
        sources = [make_source(0, "~/.claude/CLAUDE.md", "global do usuário", "user", GLOBAL_MD)]
        if (cwd / "CLAUDE.md").exists():
            sources.append(make_source(1, "./CLAUDE.md", "projeto / time", "claude", cwd / "CLAUDE.md"))
        projects.append({"name": cwd.name, "dir": str(cwd).replace(str(HOME), "~"), "sources": sources})
    return projects


# ---- Relatório no terminal (--report): orçamento de contexto sem abrir o navegador ----
def fmt_tok(n):
    """Formata tokens: 1234 -> '1.2k', 12345 -> '12k' (espelha o fmtTok do front)."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1000:.1f}k"
    return f"{round(n / 1000)}k"


def current_project():
    """Fontes do projeto do cwd (mesma composição que o discover() dá ao atual)."""
    cwd = pathlib.Path.cwd().resolve()
    sources = [make_source(0, "~/.claude/CLAUDE.md", "global do usuário", "user", GLOBAL_MD)]
    if (cwd / "CLAUDE.md").exists():
        sources.append(make_source(1, "./CLAUDE.md", "projeto / time", "claude", cwd / "CLAUDE.md"))
    mem = PROJECTS_DIR / enc_path(cwd) / "memory" / "MEMORY.md"
    if mem.exists():
        sources.append(make_source(2, "./MEMORY.md", "acumulada pelo agente", "memory", mem, base=mem.parent))
    return {"name": cwd.name, "dir": str(cwd).replace(str(HOME), "~"), "sources": sources}


def dup_index(sources):
    """Folhas cujo texto normalizado aparece em 2+ fontes (espelha o dupIndex do front)."""
    m = {}
    for s in sources:
        for t in s["topics"]:
            for it in t["items"]:
                k = re.sub(r"\s+", " ", search_norm(it["text"])).strip()
                if len(k) >= 8:                                  # ignora folhas curtas (colisão trivial)
                    e = m.setdefault(k, {"files": set(), "text": it["text"]})
                    e["files"].add(s["file"])
    return {k: e for k, e in m.items() if len(e["files"]) >= 2}


def print_report(p=None):
    """Imprime o orçamento de contexto do projeto no terminal."""
    p = p or current_project()
    sources = p["sources"]
    total = sum(s["tokens"] for s in sources)
    max_tok = max((s["tokens"] for s in sources), default=0)
    bullets = sum(len(t["items"]) for s in sources for t in s["topics"])
    w = max((len(s["file"]) for s in sources), default=10)

    print("\nMemory Map — orçamento de contexto")
    print(f"projeto: {p['name']}  ({p['dir']})\n")
    for i, s in enumerate(sorted(sources, key=lambda s: -s["tokens"])):
        nb = sum(len(t["items"]) for t in s["topics"])
        bar = "█" * (round(18 * s["tokens"] / max_tok) if max_tok else 0)
        heavy = "  ← mais pesada" if i == 0 and max_tok and len(sources) > 1 else ""
        print(f"  {s['file']:<{w}}  {bar:<18}  ~{fmt_tok(s['tokens']):>5} tok   {nb} bullets · {len(s['topics'])} tópicos{heavy}")
    print(f"  {'':<{w}}  {'':<18}  {'─' * 11}")
    print(f"  {'total':<{w}}  {'':<18}  ~{fmt_tok(total):>5} tok   {bullets} bullets · {len(sources)} fontes\n")

    dups = dup_index(sources)
    if dups:
        print(f"⧉ {len(dups)} regra(s) duplicada(s) em 2+ fontes — você paga os tokens em cada:")
        for e in sorted(dups.values(), key=lambda e: -len(e["files"])):
            txt = e["text"] if len(e["text"]) <= 72 else e["text"][:71] + "…"
            print(f"  • {txt}\n      em: {', '.join(sorted(e['files']))}")
    else:
        print("✓ nenhuma regra duplicada entre fontes.")
    print()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            data = json.dumps(discover(), ensure_ascii=False)
            self._send(200, TEMPLATE.replace("__DATA__", data))
            return
        if u.path == "/data":
            self._send(200, json.dumps(discover(), ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if u.path == "/file":
            target = unquote(parse_qs(u.query).get("p", [""])[0])
            try:
                rp = pathlib.Path(target).resolve()
                # segurança: só serve arquivos dentro de ~/.claude
                if not str(rp).startswith(str(CLAUDE_DIR)) or not rp.is_file():
                    self._send(403, "forbidden", "text/plain; charset=utf-8")
                    return
                self._send(200, rp.read_text(encoding="utf-8", errors="replace"),
                           "text/plain; charset=utf-8")
            except Exception as ex:
                self._send(500, str(ex), "text/plain; charset=utf-8")
            return
        if u.path == "/search":
            qs = parse_qs(u.query)
            q = search_norm(unquote(qs.get("q", [""])[0]).strip())
            try:
                idx = int(qs.get("p", ["0"])[0])
            except ValueError:
                idx = 0
            hits, projects = [], discover()
            if q and 0 <= idx < len(projects):
                match, seen = search_matcher(q), set()
                for s in projects[idx]["sources"]:
                    for t in s["topics"]:
                        for it in t["items"]:
                            ref = it.get("ref")
                            if ref and ref not in seen:
                                seen.add(ref)
                                body = search_file(ref)
                                if body is not None and match(body):
                                    hits.append(ref)
            self._send(200, json.dumps(hits, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    if "--report" in sys.argv[1:]:
        print_report()
        return
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    start = int(args[0]) if args else 8765
    srv = None
    port = start
    for p in range(start, start + 12):
        try:
            srv = HTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"Sem porta livre entre {start} e {start + 11}", file=sys.stderr)
        sys.exit(1)

    url = f"http://localhost:{port}"
    print(f"Memory Map em {url}  (Ctrl-C pra parar)", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
