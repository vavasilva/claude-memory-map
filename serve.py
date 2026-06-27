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


def parse_md(path, base=None):
    """Retorna [{name, items:[{text, ref?}]}]. `base` ativa captura de ref (link .md)."""
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        return []
    topics, cur, fence = [], None, False
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
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
    return [t for t in topics if t["items"]]


def make_source(b, file_label, role, tag, path, base=None):
    return {"b": b, "file": file_label, "role": role, "tag": tag,
            "topics": parse_md(path, base)}


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
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
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
