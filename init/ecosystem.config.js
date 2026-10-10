module.exports = {
  apps : [
    {
      name: "gallery",
      script: "./server.py",
      interpreter: "./venv/bin/python",
      exp_backoff_restart_delay: 100
    },
    {
      name: "gallery-workers",
      script: "./manage.py",
      args: "run_workers",
      interpreter: "./venv/bin/python",
      exp_backoff_restart_delay: 100
    }
  ]
};
