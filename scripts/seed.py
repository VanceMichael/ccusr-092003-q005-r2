"""把 fixtures/example.json 的请求序列回放到运行中的本地服务。

用法：先 make run，再 python -m scripts.seed [--base http://localhost:8080]
"""

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "example.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8080")
    args = parser.parse_args()

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for item in fixture["requests"]:
        req = urllib.request.Request(
            args.base + item["path"],
            data=json.dumps(item["body"]).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Role": item["role"]},
            method=item["method"],
        )
        try:
            with urllib.request.urlopen(req) as resp:
                status, body = resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode("utf-8")
        print(f"{item['method']} {item['path']} -> {status}")
        if status >= 400:
            print(f"    {body}")


if __name__ == "__main__":
    main()
