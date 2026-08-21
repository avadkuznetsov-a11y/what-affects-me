---
name: mira-memory-location
description: Память ревьюера по проекту «Мира» лежит в корне репозитория what-affects-me/.claude/agent-memory/code-reviewer/, а не рядом с макетами в docs/design
metadata:
  type: reference
---

Рабочая память ревьюера проекта «Мира» — в корне репозитория:
`/Users/mrwhite/what-affects-me/.claude/agent-memory/code-reviewer/`
(`MEMORY.md`, `project_mira_weak_spots.md`, `project_mira_security_rules.md`).

**Why:** ревью запускают из разных папок (иногда cwd = `docs/design`, где лежат
только макеты страницы), и каталог памяти получается пустой — можно решить, что
проект видишь впервые, и второй раз искать уже найденные грабли.

**How to apply:** перед ревью читать файлы по пути выше и обновлять их же —
новые копии памяти рядом с макетами не заводить. Сами `docs/design/` и
`docs/mockups/` к логике отношения не имеют, в ревью не входят.
