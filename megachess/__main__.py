"""Run with python -m megachess (or the megachess command)."""
import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="Play Mega Chess in your browser")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="Directory for games, learned profiles and backups")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["MEGACHESS_DATA_DIR"] = os.path.abspath(args.data_dir)
    import uvicorn
    print(f"Mega Chess → http://{args.host}:{args.port}", flush=True)
    uvicorn.run("megachess.server:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()

