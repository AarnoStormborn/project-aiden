"""``aiden providers`` — inspect, authenticate and refresh the provider suite.

Subcommands:
    list                 providers with model counts, protocols and auth state
    show <provider>      one provider's models (api, baseUrl, context, cost)
    models [pattern]     search across every provider
    check                auth + protocol coverage; exits 1 if a requested provider is unusable
    import pi            copy credentials from pi's auth.json into ~/.aiden/auth.json
    sync                 re-vendor the catalog (delegates to scripts/sync_catalog.py)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ..providers import ProviderSuite, available_apis, import_pi, status
from ..providers.registry import format_table

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The providers this project committed to supporting.
REQUIRED_PROVIDERS = ("anthropic", "openai", "opencode", "opencode-go", "deepseek", "commandcode")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiden providers", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list providers")

    show = sub.add_parser("show", help="show one provider's models")
    show.add_argument("provider")
    show.add_argument("--all", action="store_true", help="include unsupported protocols")

    models = sub.add_parser("models", help="search models")
    models.add_argument("pattern", nargs="?", default="")

    sub.add_parser("check", help="auth + protocol coverage report")

    imp = sub.add_parser("import", help="import credentials from another harness")
    imp.add_argument("source", choices=["pi"], default="pi", nargs="?")
    imp.add_argument("--overwrite", action="store_true")
    imp.add_argument("--provider", action="append", dest="providers")

    sub.add_parser("sync", help="re-vendor the catalog from a pi install")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suite = ProviderSuite.load()

    if args.command == "list":
        return cmd_list(suite)
    if args.command == "show":
        return cmd_show(suite, args.provider, include_unsupported=args.all)
    if args.command == "models":
        return cmd_models(suite, args.pattern)
    if args.command == "check":
        return cmd_check(suite)
    if args.command == "import":
        return cmd_import(args)
    if args.command == "sync":
        return cmd_sync()
    return 2


# --------------------------------------------------------------------------- commands


def cmd_list(suite: ProviderSuite) -> int:
    auth = {
        row["provider"]: row
        for row in status([p.id for p in suite.catalog.providers_with_models()])
    }
    rows = []
    for provider in suite.catalog.providers_with_models():
        apis = ",".join(sorted(provider.apis))
        supported = sorted(a for a in provider.apis if a in available_apis())
        row = auth.get(provider.id, {})
        rows.append(
            [
                provider.id,
                str(len(provider.models)),
                apis,
                "yes" if supported else "-",
                "authed" if row.get("authenticated") else "-",
                row.get("source", ""),
            ]
        )
    print(format_table(rows, ("provider", "models", "apis", "impl", "auth", "source")))
    print(f"\n{len(rows)} providers, {suite.catalog.stats()['models']} models")
    print(f"implemented protocols: {', '.join(available_apis())}")
    missing = suite.registry.missing_protocols()
    if missing:
        print(f"no transport yet: {', '.join(f'{k} ({v})' for k, v in missing.items())}")
    return 0


def cmd_show(suite: ProviderSuite, provider_id: str, *, include_unsupported: bool = False) -> int:
    try:
        provider = suite.catalog.provider(provider_id)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    cred = suite.credential(provider_id)
    print(f"# {provider.id}")
    print(f"auth: {'yes via ' + cred.source if cred else 'none'}")
    if cred and cred.expired:
        print("warning: stored credential is expired")

    rows = []
    for model in sorted(provider.models.values(), key=lambda m: (m.api, m.id)):
        if not include_unsupported and model.api not in available_apis():
            continue
        rows.append(
            [
                model.id,
                model.api,
                f"{model.context_window // 1000}k" if model.context_window else "-",
                f"{model.max_tokens // 1000}k" if model.max_tokens else "-",
                "yes" if model.reasoning else "-",
                f"{model.cost.input}/{model.cost.output}",
            ]
        )
    if not rows:
        print("no models with an implemented protocol (use --all)")
        return 0
    print(format_table(rows, ("model", "api", "context", "max_out", "reason", "in/out $/M")))
    print(f"\nbase URLs: {', '.join(sorted({m.base_url for m in provider.models.values()}))}")
    return 0


def cmd_models(suite: ProviderSuite, pattern: str) -> int:
    models = (
        suite.search(pattern)
        if pattern
        else [m for p in suite.catalog.providers_with_models() for m in p.models.values()]
    )
    supported = [m for m in models if m.api in available_apis()]
    rows = [
        [
            m.ref,
            m.api,
            f"{m.context_window // 1000}k" if m.context_window else "-",
            "yes" if m.reasoning else "-",
        ]
        for m in sorted(supported, key=lambda m: (m.provider, m.id))
    ]
    print(format_table(rows, ("model", "api", "context", "reason")))
    print(f"\n{len(rows)} models" + (f" matching '{pattern}'" if pattern else ""))
    return 0


def cmd_check(suite: ProviderSuite) -> int:
    rows = status(list(REQUIRED_PROVIDERS))
    problems: list[str] = []

    for row in rows:
        provider = row["provider"]
        try:
            info = suite.catalog.provider(provider)
        except KeyError:
            problems.append(f"{provider}: not in catalog")
            continue
        supported = [a for a in info.apis if a in available_apis()]
        if not supported:
            problems.append(f"{provider}: no implemented protocol for {sorted(info.apis)}")
        if not row["authenticated"]:
            problems.append(f"{provider}: not authenticated")
    print(
        format_table(
            [
                [
                    r["provider"],
                    "yes" if r["authenticated"] else "no",
                    r["type"],
                    r["source"],
                    r["key"],
                ]
                for r in rows
            ],
            ("provider", "authed", "type", "source", "key"),
        )
    )

    unsupported = suite.registry.missing_protocols()
    if unsupported:
        print(f"\nprotocols without a transport: {', '.join(unsupported)}")

    if problems:
        print("\nissues:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\nall required providers usable")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        written = import_pi(providers=args.providers, overwrite=args.overwrite)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not written:
        print("nothing imported (already present; use --overwrite)")
        return 0
    from ..providers.auth import auth_file_path

    print(f"imported from pi: {', '.join(written)}")
    print(f"written to {auth_file_path()}")
    return 0


def cmd_sync() -> int:
    script = REPO_ROOT / "scripts" / "sync_catalog.py"
    if not script.is_file():
        print(f"sync script not found: {script}", file=sys.stderr)
        return 1
    # S603: argv is built from a fixed repo-relative path, never user input.
    return subprocess.call([sys.executable, str(script)])  # noqa: S603


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
