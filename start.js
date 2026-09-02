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
          PINOKIO_HOME: "{{path.resolve(cwd, '..')}}",
        },
        message: [
          "python3 arbiter/demeter-gpu-arbiter.py || python arbiter/demeter-gpu-arbiter.py",
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
        description: "Dashboard + API — port 8790",
      },
    },
  ],
}
