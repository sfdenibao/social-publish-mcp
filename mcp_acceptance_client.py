"""Acceptance commands use real MCP tools/call, never import uploader internals."""
import argparse
import asyncio
import json
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('tool')
    parser.add_argument('--args-file')
    parser.add_argument('--output')
    parser.add_argument('--url', default='http://127.0.0.1:5410/mcp')
    args = parser.parse_args()
    payload = json.loads(Path(args.args_file).read_text(encoding='utf-8-sig')) if args.args_file else {}
    async with streamable_http_client(args.url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool(args.tool, payload)
            data = result.model_dump(mode='json')
            if args.output:
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(data, ensure_ascii=False))
            failed = result.isError
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
