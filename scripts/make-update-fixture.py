"""Create a localhost-only manifest from one signed native build, optionally corrupting its copy."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import quote, unquote, urlparse

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("platform_json",type=Path)
parser.add_argument("--output",type=Path,required=True)
parser.add_argument("--port",type=int,default=17891)
parser.add_argument("--tamper",action="store_true",help="Corrupt a copied asset; the updater must reject it")
parser.add_argument("--announced-version",help="Override only the manifest version to test signed-version rejection")
args=parser.parse_args()
if not 1 <= args.port <= 65535: parser.error("Port must be in 1..65535")
part=json.loads(args.platform_json.read_text(encoding="utf-8"))
name=unquote(urlparse(part["url"]).path.rsplit("/",1)[-1])
if not name or Path(name).name != name or "/" in name or "\\" in name: parser.error("Unsafe asset filename")
source=args.platform_json.parent/name
expected=next((item["sha256"] for item in part["files"] if item["name"]==name),None)
with source.open("rb") as stream: actual=hashlib.file_digest(stream,"sha256").hexdigest()
if actual!=expected: parser.error("Original artifact failed its recorded checksum")
destination=args.output/name
if source.resolve()==destination.resolve(): parser.error("Fixture output must be separate from the original artifacts")
args.output.mkdir(parents=True,exist_ok=True)
shutil.copy2(source,destination)
if args.tamper:
    with destination.open("r+b") as stream:
        first=stream.read(1)
        if not first: parser.error("Empty artifact")
        stream.seek(0)
        stream.write(bytes([first[0]^1]))
manifest={"version":args.announced_version or part["version"],"notes":"本机更新演练；此清单不能用于正式分发。","platforms":{part["platform"]:{"url":f"http://127.0.0.1:{args.port}/{quote(name)}","signature":part["signature"]}}}
(args.output/"latest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(f"Fixture prepared at {args.output}; signature tampering={args.tamper}, version override={args.announced_version or 'none'}")
