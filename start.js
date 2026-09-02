module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        env: {
          GPU_ARBITER_HOST: "0.0.0.0",
          GPU_ARBITER_PORT: "8790",
          GPU_ARBITER_UI_DIST: "{{cwd}}/ui/dist",
          // Parent = api/ ; home Pinokio = grand-parent (…/pinokio)
          PINOKIO_HOME: "{{path.resolve(cwd, '../..')}}",
        },
        // Idempotent : si systemd (ou une autre instance) tient déjà :8790, ne pas rebind
        message: [
          "bash -lc 'if curl -sf http://127.0.0.1:8790/health >/dev/null 2>&1; then echo \"Demeter GPU Arbiter on 0.0.0.0:8790 (already running)\"; exit 0; fi; exec python3 arbiter/demeter-gpu-arbiter.py || exec python arbiter/demeter-gpu-arbiter.py'",
        ],
        on: [{
          event: "/Demeter GPU Arbiter on/i",
          done: true,
        }],
      },
    },
    {
      method: "local.set",
      params: {
        url: "http://127.0.0.1:8790",
        port: 8790,
      },
    },
    {
      method: "process.wait",
      params: {
        url: "http://127.0.0.1:8790/health",
        interval: 2,
        title: "GPU Arbiter",
        description: "Dashboard — http://127.0.0.1:8790/",
      },
    },
    {
      method: "process.wait",
      params: {
        title: "GPU Arbiter",
        description: "Actif — UI + API sur :8790 (systemd ou Pinokio)",
      },
    },
  ],
}
