# cli.main is implemented in Task 7; stub until then so imports work.
def _stub_main(argv=None):
    raise SystemExit("CLI not implemented yet — continue Phase 0 tasks")


if __name__ == "__main__":
    try:
        from agentic_trading.cli import main
    except ImportError:
        main = _stub_main
    raise SystemExit(main())
