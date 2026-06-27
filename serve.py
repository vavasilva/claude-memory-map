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
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HOME = pathlib.Path.home()
CLAUDE_DIR = (HOME / ".claude").resolve()
GLOBAL_MD = CLAUDE_DIR / "CLAUDE.md"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATE = (PLUGIN_DIR / "template.html").read_text(encoding="utf-8")


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


def discover():
    """Monta a lista de projetos. Projeto atual (cwd) vem primeiro e ganha as 3 fontes;
    os demais ganham global + MEMORY.md (o CLAUDE.md do repo deles não é resolvível pelo
    nome codificado do diretório)."""
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
        current = (e == cwd_enc)
        if current:
            name = cwd.name
            dirlabel = str(cwd).replace(str(HOME), "~")
        else:
            name = e.split("-Code-")[-1] if "-Code-" in e else e.strip("-").split("-")[-1]
            dirlabel = "~/.claude/projects/" + name
        sources = [make_source(0, "~/.claude/CLAUDE.md", "global do usuário", "user", GLOBAL_MD)]
        if current and (cwd / "CLAUDE.md").exists():
            sources.append(make_source(1, "./CLAUDE.md", "projeto / time", "claude", cwd / "CLAUDE.md"))
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
