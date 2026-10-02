# Makerspace Gamification agent handbook

## 1. Repository purpose and boundaries

The gamification overly adds gamification for  members, turning their activities into a gamified cultivation path, where members gain skills and experience points (xp) through their makerspace activities, checkouts, volunteer work, and the like.
 
This, `makerspace-react-2026` and `makerspace-rails-2026` are independent Git checkouts. 
Inspect `git status --short`, preserve existing changes, and set an explicit working
directory for each command. Backend-only work does not require a sibling checkout 
This is the canonical handbook; keep overlapping companion instructions aligned.

| Tool | Entry point |
| --- | --- |
| Codex | Root `AGENTS.md` via [native discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md). Sessions launched from the enclosing workspace must explicitly read the applicable repository handbook before working there. |
| Claude Code | [CLAUDE.md](CLAUDE.md) imports this file with unquoted `@AGENTS.md`, including for sessions without native AGENTS loading; no symlink is needed. See [memory imports](https://code.claude.com/docs/en/memory). |
| Cursor | Read root `AGENTS.md` directly using [Cursor rules](https://cursor.com/docs/rules); no additional Cursor rules file. |
| GitHub Copilot | [.github/copilot-instructions.md](.github/copilot-instructions.md) summarizes critical rules and directs readers here. Its link is not an automatic import; [support varies by surface](https://docs.github.com/en/copilot/reference/custom-instructions-support). |

## 2. Setup and common commands

  
## 3. Architecture and implementation conventions

### Current layout
 
Mongoid is the persistence layer.  

 

## 4. Task-specific documentation

### Authoritative documentation-maintenance table

Documentation updates are part of implementation, including those required by an
already-authorized task. 

## 5. Testing and completion criteria

 

### Test environment and assets

 
### Transaction coverage
 
## 6. Cross-repository changes

Inspect all checkouts/handbooks for features crossing the boundary. Trace Rails
routes/controllers/policies/serializers/specs through React wrappers, types,
capabilities, and screens. Shared auth needs password/TOTP/session/CSRF tests on
both sides; public resources also need visibility/format tests.
 
## 7. Operational constraints and common pitfalls
 
