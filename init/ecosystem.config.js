module.exports = {
  apps : [
  {
      name: "gallery",
      script: "./server.py",
      interpreter: "./venv/bin/python",
      exp_backoff_restart_delay: 100
    }]
};
