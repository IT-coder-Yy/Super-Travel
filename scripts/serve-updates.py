"""Local-only update fixture server. Never use --bind 0.0.0.0 for rehearsal artifacts."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory",type=Path)
parser.add_argument("--port",type=int,default=17891)
args=parser.parse_args()
if not args.directory.is_dir(): parser.error("Fixture directory does not exist")
handler=partial(SimpleHTTPRequestHandler,directory=str(args.directory.resolve()))
server=ThreadingHTTPServer(("127.0.0.1",args.port),handler)
print(f"Local update fixture: http://127.0.0.1:{server.server_port}/latest.json",flush=True)
try: server.serve_forever()
except KeyboardInterrupt: pass
finally: server.server_close()
