# 0004. One Pi per session

One Pi RPC process (or guest) per session. After `APIPI_IDLE_TTL`
(default 15 minutes) with no turn, kill the process. The session row
stays. Resume from our event log, not Pi's files.
