module.exports = {
  title: "GPU Arbiter",
  description: "Time-sharing GPU LLM/ACE/Wan + priorité Steam — UI sur :8790",
  icon: "icon.svg",
  menu: async (kernel, info) => {
    const installed = info.exists("ui/dist/index.html")
    const running = info.running("start.js")
    const local = info.local("start.js")

    if (!installed) {
      return [{
        default: true,
        icon: "fa-solid fa-download",
        text: "Install",
        href: "install.js",
      }]
    }

    if (running && local && local.url) {
      return [
        { default: true, icon: "fa-solid fa-globe", text: "Open Dashboard", href: local.url },
        { icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" },
        { icon: "fa-solid fa-rotate", text: "Reinstall", href: "install.js" },
      ]
    }

    return [
      { default: true, icon: "fa-solid fa-play", text: "Start", href: "start.js" },
      { icon: "fa-solid fa-rotate", text: "Reinstall", href: "install.js" },
    ]
  },
}
