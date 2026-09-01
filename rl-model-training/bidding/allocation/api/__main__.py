"""``python -m allocation.api`` — serve the allocation engine over HTTP.

::

    python -m allocation.api
    python -m allocation.api --port 9000 --config-dir ./my-config
    curl -s localhost:8000/health

Binds to **127.0.0.1** by default. Anything reachable on the network can run auctions and
read the full derivation of every one, so the default has to be loopback and exposing it is
an explicit ``--host`` away.

**Every flag has an environment variable**, because that is how a container is configured —
a Dockerfile that has to rewrite a CMD to change a port is a Dockerfile people fork instead
of parameterising. The flag wins where both are given, so a compose file can set a default
that an operator can still override at ``docker run``.

**One worker, deliberately.** :class:`~allocation.api.service.RunStore` lives in process
memory, so under two workers a ``POST /auction`` handled by one and the follow-up
``GET /auction/{id}`` routed to the other returns a 404 that looks exactly like an expired
entry. Scale by running more containers behind a balancer, or accept that retrieval is
best-effort — but do not silently turn on workers and leave the store in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from allocation.api import service


def _env(name: str, default: str | None = None) -> str | None:
    """Read ``ALLOCATION_<NAME>``, treating blank as unset.

    Blank matters: ``ALLOCATION_API_KEY=`` in a compose file or an unset CI secret arrives as
    an empty string, and an empty string is falsy in exactly the way that would silently turn
    authentication *off* while looking configured.
    """
    value = os.environ.get(f"ALLOCATION_{name}", "").strip()
    return value or default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allocation.api",
        description="Serve the HOSPILOT allocation auction over HTTP.",
    )
    parser.add_argument(
        "--host",
        default=_env("HOST", "127.0.0.1"),
        help="bind address (default loopback; $ALLOCATION_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env("PORT", "8000")),
        help="bind port (default 8000; $ALLOCATION_PORT)",
    )
    parser.add_argument(
        "--config-dir",
        default=_env("CONFIG_DIR"),
        metavar="PATH",
        help="alternative config directory, fixed for the process — requests cannot change it",
    )
    parser.add_argument(
        "--scenario-dir",
        default=_env("SCENARIO_DIR", "scenarios"),
        metavar="PATH",
        help="directory scenarios are read from; requests address them by name only",
    )
    parser.add_argument(
        "--store-size",
        type=int,
        default=int(_env("STORE_SIZE", str(service.DEFAULT_STORE_SIZE))),
        metavar="N",
        help=f"runs kept in memory for GET /auction/{{id}} (default {service.DEFAULT_STORE_SIZE})",
    )
    parser.add_argument(
        "--policy",
        default=_env("POLICY"),
        metavar="PATH",
        help="trained weights to serve under 'policy: rl' ($ALLOCATION_POLICY). Fixed for the "
             "process — a request selects the policy, never the file. Refuses weights fitted "
             "under a different encoder, at startup rather than per request",
    )
    parser.add_argument(
        "--live-policy",
        action="store_true",
        default=_env("LIVE_POLICY") is not None,
        help="let --policy actually decide allocations ($ALLOCATION_LIVE_POLICY). Without it "
             "'policy: rl' is SHADOWED: the heuristic allocates and the learned choices are "
             "recorded in the response",
    )
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    return parser


def _announce_policy(args, api_key: str | None) -> None:
    """Say what the process will serve, before it serves it.

    The CLI prints the provisional-safety banner on every run because a person is watching one
    auction. A server runs thousands unattended, so the banner belongs at startup, where the
    log keeps it — and ``/health`` carries the same facts for anyone who arrives later.
    """
    if not args.policy:
        return
    print(
        f"policy   {args.policy}\n"
        f"         {'ACTING — a learned policy will decide allocations'
                   if args.live_policy else
                   'shadowed — the heuristic allocates, learned choices are recorded'}",
        file=sys.stderr,
    )
    if args.live_policy:
        print(
            "!  A learned policy is deciding allocations behind PROVISIONAL constraints.\n"
            "!  The reward it optimises has never been fitted and no_mortality has no source\n"
            "!  (F-01, RL_FIXES C-5). This is not a deployment recommendation.",
            file=sys.stderr,
        )
        if not api_key:
            print(
                "!  No ALLOCATION_API_KEY: anyone who can reach this port can have a learned\n"
                "!  policy decide an allocation.",
                file=sys.stderr,
            )


def main(argv: Sequence[str] | None = None) -> int:
    # RL_FIXES fix 7, which patched the CLI and not this entry point. A server's startup lines
    # go to a log file or `docker logs` far more often than to a terminal, so on Windows the
    # cp1252 default mangles the safety banner in exactly the case where it is most needed —
    # the unattended one. Reuses the CLI's helper rather than a second copy of the reasoning.
    from allocation.cli import _force_utf8

    _force_utf8()
    args = build_parser().parse_args(argv)

    try:
        import uvicorn
    except ModuleNotFoundError:
        print(
            'the HTTP API needs FastAPI and uvicorn: pip install -e ".[api]"',
            file=sys.stderr,
        )
        return 1

    from allocation.api.app import create_app

    api_key = _env("API_KEY")

    try:
        app = create_app(
            config_dir=Path(args.config_dir) if args.config_dir else None,
            scenario_dir=Path(args.scenario_dir),
            store_size=args.store_size,
            api_key=api_key,
            policy_path=Path(args.policy) if args.policy else None,
            policy_live=args.live_policy,
        )
    except service.ApiError as exc:
        print(f"cannot start: {exc.message}", file=sys.stderr)
        return 1

    if args.host not in ("127.0.0.1", "localhost", "::1") and not api_key:
        # Not fatal: inside a container 0.0.0.0 is the only useful bind, and the fixture data
        # is three invented patients. Loud, though, because the line between "public demo" and
        # "public hospital endpoint" is one DataSource swap and nothing in the code marks it.
        print(
            f"! binding {args.host} with no ALLOCATION_API_KEY set — anyone who can reach "
            f"port {args.port} can run auctions. Fine for fixtures; set the key before a real "
            f"DataSource is wired.",
            file=sys.stderr,
        )

    _announce_policy(args, api_key)

    print(
        f"docs on http://{args.host}:{args.port}/docs   "
        f"auth {'api_key' if api_key else 'none'}",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
