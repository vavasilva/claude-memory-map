---
description: Mapa mental das memórias do Claude Code (localhost) — ou --report no terminal
argument-hint: '[porta | --report]'
disable-model-invocation: true
allowed-tools: Bash
---

A partir do diretório de trabalho atual do projeto (NÃO faça `cd` — o servidor precisa do cwd
pra detectar o projeto atual):

**Se `$ARGUMENTS` contiver `--report`**, rode em **foreground** e mostre a saída ao usuário. É um
relatório curto que imprime e encerra (orçamento de tokens por fonte, total, fonte mais pesada e
regras duplicadas entre fontes):

```
python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" --report
```

**Caso contrário**, inicie o servidor do **Memory Map** em **background**:

```
python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" $ARGUMENTS
```

O servidor:
- detecta o projeto atual pelo diretório de trabalho e lê as 3 fontes de memória **ao vivo**
  (`~/.claude/CLAUDE.md`, `./CLAUDE.md`, `./MEMORY.md`);
- lista também os outros projetos com memória em `~/.claude/projects/*/memory/`;
- sobe em `http://localhost:8765` (ou a próxima porta livre) e abre o navegador.

Rode o servidor em background pra não travar a sessão. Depois informe ao usuário a URL e que, pra
parar, basta matar o processo (`kill` do PID em background, ou `Ctrl-C` se rodar no terminal).
