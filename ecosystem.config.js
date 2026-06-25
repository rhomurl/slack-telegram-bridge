module.exports = {
  apps: [
    {
      name: "slack-telegram-bridge",
      script: "slack_to_telegram_bridge.py",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
