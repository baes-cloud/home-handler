import argparse
import asyncio
import logging
import os
import sys

from . import DATA_DIR, config
from .journal import Journal

JOURNAL_PATH = os.environ.get("HOUSEAGENT_JOURNAL") or os.path.join(DATA_DIR, "journal.db")


def make_ctx(cfg, token_path=None, need_ha=True):
    from .ha import HAClient
    from .watchers.base import Context
    ha = None
    if need_ha:
        ha = HAClient(cfg["ha"]["ws_url"], cfg["ha"]["rest_url"], token_path or cfg["ha"]["token_path"])
    return Context(Journal(JOURNAL_PATH), cfg, ha=ha)


async def _watch(args, cfg):
    from . import loose_ends
    from .runner import Runner
    from .watchers import load_all
    ctx = make_ctx(cfg, args.token)
    watchers = [cls() for cls in load_all()]
    for w in watchers:
        if hasattr(w, "attach"):
            w.attach(ctx)
    runner = Runner(ctx, watchers)
    if args.once:
        task = asyncio.create_task(ctx.ha.run(runner.handle_event, ["state_changed", "homeassistant_started"],
                                              on_connect=runner.on_connect, on_disconnect=runner.on_disconnect))
        await asyncio.wait_for(ctx.ha.connected.wait(), 30)
        await asyncio.sleep(5)  # let some events flow
        await runner.tick(force=True, only=set(args.only) if args.only else None)
        task.cancel()
        for s in ctx.journal.watcher_stats():
            print(f"{s['name']}: runs={s['runs']} fires={s['fires']} errors={s['errors']} last_error={s['last_error']}")
        return
    asyncio.create_task(loose_ends.sync_forever(ctx))
    await runner.run_forever()


def main(argv=None):
    p = argparse.ArgumentParser(prog="home_handler")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--once", action="store_true")
    w.add_argument("--only", nargs="*")
    w.add_argument("--token", help="HA token file (default from config)")
    for name in ("web", "evening", "replay", "sort-loose-ends"):
        sp = sub.add_parser(name)
        sp.add_argument("--token")
        if name == "web":
            sp.add_argument("--port", type=int, default=8790)
            sp.add_argument("--allow", nargs="*", default=["192.168.1.10"])
            sp.add_argument("--aud", default=os.environ.get("ACCESS_AUD", ""))
        if name == "replay":
            sp.add_argument("--days", type=int, default=10)
        if name == "sort-loose-ends":
            g = sp.add_mutually_exclusive_group(required=True)
            g.add_argument("--dry-run", action="store_true", help="ask Claude, show and save the proposed sort")
            g.add_argument("--apply", action="store_true", help="apply the saved preview exactly (backs up data.json)")
        if name == "evening":
            sp.add_argument("--force", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    cfg = config.load(args.config)
    if args.cmd == "watch":
        asyncio.run(_watch(args, cfg))
    elif args.cmd == "web":
        from . import web
        web.serve(Journal(JOURNAL_PATH), cfg, args.port, set(args.allow), args.aud)
    elif args.cmd == "evening":
        from . import evening
        print(asyncio.run(evening.main(make_ctx(cfg, args.token), force=args.force)))
    elif args.cmd == "replay":
        from . import replay
        asyncio.run(replay.main(make_ctx(cfg, args.token), args.days))
    elif args.cmd == "sort-loose-ends":
        from . import loose_ends
        ctx = make_ctx(cfg, need_ha=False)
        if args.dry_run:
            print(loose_ends.sync(ctx.journal, cfg), "new")
            print("proposal saved:", loose_ends.sort_open(ctx.journal, cfg, dry_run=True))
            print("Apply exactly this with: python3 -m home_handler sort-loose-ends --apply")
        else:
            res = loose_ends.apply_proposal(ctx.journal, cfg)
            print("applied:", res if res is not None else "no saved preview; run --dry-run first")


if __name__ == "__main__":
    main()
