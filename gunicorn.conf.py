# gunicorn reads this file automatically, so a bare `gunicorn app:app` (railway.toml) gets these settings.
#
# One gthread worker: audits live in this process's memory, and each live-progress page holds a
# long-lived SSE connection. Gunicorn's default (one sync worker, 30s timeout) would block every
# other request while a stream is open and kill the stream after 30 seconds.
workers = 1
worker_class = "gthread"
threads = 16
timeout = 120
# No `bind` here: gunicorn listens on 0.0.0.0:$PORT when PORT is set (Railway), else 127.0.0.1:8000.
