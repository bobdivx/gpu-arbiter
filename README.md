# GPU Arbiter — app Pinokio (Astro + Python)

Time-sharing GPU **LLM / ACE / Wan** avec priorité **Steam**, dashboard web, installable via Pinokio.

## Architecture

Un seul process runtime : `arbiter/demeter-gpu-arbiter.py` sur **`:8790`**

- API JSON : `GET /status`, `POST /acquire|release|touch`, `GET /health`
- UI Astro (build `ui/dist`) servie sur `/` (même origine)

## Pinokio

1. Copier ce dossier sous `$PINOKIO_HOME/api/gpu-arbiter/` (ou Discover / clone GitHub)
2. **Install** → `npm install` + `npm run build` dans `ui/`
3. **Start** → ouvre `http://127.0.0.1:8790/`

Variables `ENVIRONMENT` (héritables) :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `GPU_ARBITER_PORT` | `8790` | Port HTTP |
| `GPU_ARBITER_HOST` | `0.0.0.0` | Bind |
| `GPU_ARBITER_DEFAULT` | `llm` | Slot après idle / fin Steam |
| `GPU_ARBITER_STEAM_PRIORITY` | `1` | `0` pour désactiver |
| `GPU_ARBITER_UI_DIST` | `../ui/dist` | Build Astro |
| `PINOKIO_HOME` | `/mnt/ia/pinokio` | Scripts ACE / LLM |
| `DEMETER_REPO` | `~/Documents/devforge` | Scripts bootstrap |

## API

```bash
curl -s http://127.0.0.1:8790/status | jq
curl -s -X POST http://127.0.0.1:8790/acquire \
  -H 'Content-Type: application/json' \
  -d '{"slot":"ace","owner":"ui","timeout_s":600}'
```

CLI : `arbiter/demeter-gpu` → `status | use llm|ace|wan | release`

## Dev UI (local)

```bash
cd ui
npm install
npm run dev    # :4321, proxy API → :8790
npm run build  # → dist/
```

## Demeter (systemd)

Le dossier `scripts/demeter-bootstrap/gpu-arbiter/` reste le point d’entrée install systemd.
Il synchronise / pointe vers **ce package** (source of truth).

```bash
# sync arbiter + UI build vers le service
bash scripts/demeter-bootstrap/gpu-arbiter/sync-from-pinokio-package.sh
```

Dashboard : `http://10.1.0.88:8790/`

## Partage

Repo public : **https://github.com/bobdivx/gpu-arbiter**

Pinokio → Discover / clone cette URL, puis Install + Start.
