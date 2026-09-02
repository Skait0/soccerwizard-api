"""Gunicorn settings, in the one place Railway actually reads.

WHY THIS FILE EXISTS AND THE PROCFILE DOES NOT DO THE JOB.

The Procfile was given `--worker-class gthread --threads 8` on 2 Sep 2026 and
the container came up saying `Using worker: sync` thirty-six seconds later.
Railway is not running the Procfile - the start command is configured on the
service - so editing the Procfile changed nothing at all, silently, and the
before/after measurements around it were noise.

Gunicorn loads `gunicorn.conf.py` from the working directory automatically,
whatever the command line says. Explicit command-line flags still win, so
anything the service's start command sets stays in charge; everything it does
not set is settled here. That makes this file effective without dashboard
access, which the Procfile was not.

Verify after deploying by reading the boot line, not by timing requests:

    railway logs --service web | grep "Using worker"

It must say `gthread`. If it says `sync`, the start command is passing an
explicit worker class and this file is being overridden.

--------------------------------------------------------------------------
WHY THREADS AND NOT WORKERS

Both background refreshers start at module import (see _start_fixtures_thread
in server.py), and gunicorn imports the app once PER WORKER. A second worker
would start a second fifty-six-request SportyBet sweep and a second Bet9ja
sweep, against endpoints that already refuse bursts from datacentre IPs.
Raising the worker count to make booking feel faster is how a slowness problem
becomes an outage.

One process keeps one refresher and one cache. Threads give concurrent request
handling, and the work is entirely I/O - waiting on SportyBet, Bet9ja and
Redis - so the GIL costs nothing here.

Nothing new is exposed to concurrency by this: the refresher threads already
write the caches while request handlers read them. `_cache_get`/`_cache_put`
touch a module dict and Redis, and `_ODDS_LOOKUP` is built once at import and
read-only afterwards.

WHAT IT FIXES

A booking is one upstream POST; validation is cache-only and makes no request
at all. With a single sync worker, that POST queued behind whatever the page
was already fetching - and the page opens by fetching /api/fixtures,
/api/live and /api/bet9ja. Measured against production before the change: six
concurrent hits on the trivial `/` endpoint fanned out to 9.5s and one never
returned inside two minutes, while the same six against Cloudflare from the
same machine were flat at 0.51-0.56s. The wait was ours, not the network's and
not SportyBet's.
"""

workers = 1
worker_class = "gthread"
threads = 8

# Unchanged from the original start command, restated so they survive if the
# service's command is ever simplified to just `gunicorn server:app`.
timeout = 90
graceful_timeout = 30

# Say the configuration out loud at boot, so the next person does not have to
# infer it from response times the way this one did.
def on_starting(server):
    server.log.info(
        "soccerwizard: workers=%s worker_class=%s threads=%s "
        "(one process on purpose - the refreshers start per import)",
        workers, worker_class, threads)
