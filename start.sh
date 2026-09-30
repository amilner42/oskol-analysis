#!/bin/sh
# Join the tailnet if we were given a key, then serve.
#
# With TAILSCALE_AUTHKEY set this container can reach the desktop the engine
# runs on (Aveline ticket bg-analysis-imac) and UPSTREAM_URL points at it.
# Without one, nothing below runs and this is the app it always was: the
# engine, computing here. That is the rollback, and it is why the key and the
# upstream are two separate switches rather than one.
set -eu

if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
  # Userspace networking: a Fly machine has no TUN device, so tailscaled
  # cannot put a route in the kernel. Nothing here reaches the tailnet by
  # opening an ordinary socket — outbound connections go through the proxy
  # below, which is what UPSTREAM_PROXY names in fly.toml.
  #
  # --state=mem: because the auth key is ephemeral: this machine stops when
  # idle and starts on the next request, so it joins afresh every time and a
  # node that outlived its container would only be litter in the admin
  # console.
  tailscaled \
    --tun=userspace-networking \
    --state=mem: \
    --outbound-http-proxy-listen=localhost:1055 &

  # --accept-routes=false: we want one host, not its network. The ACL grants
  # this node aries-imac:8080 and nothing else; accepting subnet routes would
  # quietly widen that on the client side.
  tailscale up \
    --authkey="${TAILSCALE_AUTHKEY}" \
    --hostname="${TAILSCALE_HOSTNAME:-oskol-analysis-fly}" \
    --accept-routes=false
fi

# One server worker: it answers the single-position routes from engines it
# loads once. A review fans its turns out over a process pool of its own
# (app/pool.py), one engine process per core. Forwarding, it loads neither.
exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
