---
description: Orçamento de contexto no terminal — tokens por fonte, total, fonte mais pesada e regras duplicadas
disable-model-invocation: true
allowed-tools: Bash
---

Rode em **foreground** (a partir do diretório de trabalho atual do projeto — NÃO faça `cd`, o
relatório precisa do cwd pra detectar o projeto atual) e mostre a saída ao usuário. É um relatório
curto que imprime e encerra:

```
python3 "${CLAUDE_PLUGIN_ROOT}/serve.py" --report
```

Mostra, pro projeto atual: tokens (~estimados) por fonte, total, a **fonte mais pesada** e as
**regras duplicadas** entre fontes (a mesma instrução no global e no `./CLAUDE.md`, por exemplo) —
candidatas a enxugar.
