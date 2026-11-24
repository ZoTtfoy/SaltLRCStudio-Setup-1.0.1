import argparse
from .batch import run_batch
from .server import serve


def main():
    parser = argparse.ArgumentParser(description="SaltLRC Studio")
    sub = parser.add_subparsers(dest="command")
    ui = sub.add_parser("serve", help="启动本地服务并在浏览器中打开（需要自己开浏览器时用）")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--models", help="本地模型根目录")
    ui.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    win = sub.add_parser("window", help="以独立窗口打开编辑器（推荐）")
    win.add_argument("--port", type=int, default=None, help="默认自动选空闲端口")
    win.add_argument("--models", help="本地模型根目录")
    win.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    win.add_argument("--debug", action="store_true", help="打开 WebView2 开发者工具")
    batch = sub.add_parser("batch", help="批量修复并导出")
    batch.add_argument("source")
    batch.add_argument("output")
    batch.add_argument("--format", choices=["salt", "lrc"], default="salt")
    batch.add_argument("--encoding", help="显式指定输入编码，如 shift_jis")
    args = parser.parse_args()
    if args.command == "batch":
        try:
            rows = run_batch(args.source, args.output, args.format == "salt", args.encoding)
        except (ValueError, OSError) as error:
            parser.exit(2, str(error) + "\n")
        for row in rows:
            print(f"{row['status']}: {row['file']}")
            for error in row["errors"]:
                print("  " + error)
        print(f"Exported {sum(r['status'] == 'exported' for r in rows)}/{len(rows)}")
        return 1 if any(r["status"] == "failed" for r in rows) else 0
    if args.command == "window":
        from . import window as window_module
        if not window_module.available():
            raise SystemExit("独立窗口不可用：需要 Windows 且已安装 pywebview（pip install pywebview）")
        return window_module.run_window(port=args.port, models=args.models,
                                       device=args.device, debug=args.debug)
    serve(getattr(args, "port", 8765), not getattr(args, "no_browser", False),
          getattr(args, "models", None), getattr(args, "device", "cpu"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
