module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        message: "python3 --version || python --version",
      },
    },
    {
      method: "shell.run",
      params: {
        path: "ui",
        message: [
          "npm install",
          "npm run build",
        ],
      },
    },
    {
      method: "log",
      params: {
        text: "GPU Arbiter installé. Lance Start — dashboard http://127.0.0.1:8790/",
      },
    },
  ],
}
