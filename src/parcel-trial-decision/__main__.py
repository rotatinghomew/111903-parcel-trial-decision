"""命令行入口：跑两份台账的审阅与核对，输出报告。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .ledger import load_ledger
from .report import build_report, render_text


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="parcel-trial-decision",
        description="合箱现场试验审阅与承运组合扩围核对",
    )
    parser.add_argument("--data-dir", default="data", help="台账 JSON 所在目录")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把机器可读报告写到该路径")
    parser.add_argument("--today", default=None,
                        help="当前日期（YYYY-MM-DD），默认取系统日期")
    args = parser.parse_args()

    from datetime import date
    today = args.today or date.today().isoformat()

    data_dir = Path(args.data_dir)
    field = route = None
    field_path = data_dir / "field_waves.json"
    route_path = data_dir / "route_trial.json"
    if field_path.exists():
        field = load_ledger(field_path, name="field_waves")
    if route_path.exists():
        route = load_ledger(route_path, name="route_trial")

    report = build_report(field, route, today)
    print(f"本地分析工作区 {__version__}")
    print(render_text(report))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n机器可读报告已写入 {args.json_path}")


if __name__ == "__main__":
    main()
